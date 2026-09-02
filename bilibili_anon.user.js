// ==UserScript==
// @name         B站截图打码助手
// @namespace    anon.bilibili
// @version      0.5.8
// @description  左下角 ｢码｣ 按钮或 Alt+M 进入打码编辑态：编辑态禁用页面一切跳转/点击动作；点头像或用户名即同时盖圆+替换 ｢用户首字母｣。头像与用户名链接同一 mid，共用同一档案：颜色（用户名拼音首字母）恒一致，无任何弹窗输入。仅本次页面生效，不写任何持久化存储，刷新即清空。覆盖视频/动态(opus)/专栏(read)页面。
// @match        https://www.bilibili.com/video/*
// @match        https://www.bilibili.com/opus/*
// @match        https://www.bilibili.com/read/*
// @license      GNU AGPLv3
// @run-at       document-idle
// @grant        none
// ==/UserScript==
(function () {
    'use strict';

    // ---------------- 映射（仅本次页面生效，刷新即清空，不写任何存储） ----------------
    // 头像与用户名的链接指向同一 space mid，档案键统一用 mid:数字（无 mid 才退 name:名字），
    // 无论先点头像还是先点名字，编号/颜色/字母都指向同一档案
    const users = {};
    let autoApply = true;

    // 拼音首字母：Chrome 的 zh collation 即拼音序，用 23 个声母锚点字二分（拼音无 I U V）。
    // 锚点取各声母区间最靠前的代表字（同音字中笔画少的在前，如 七 在 期 前），避免区间丢失
    const PY_ANCHORS = ['阿', '八', '嚓', '搭', '蛾', '发', '噶', '哈', '击', '喀', '垃', '妈', '拿', '哦', '啪', '七', '然', '撒', '他', '挖', '西', '压', '匝'];
    const PY_LETTERS = 'ABCDEFGHJKLMNOPQRSTWXYZ';
    const zhColl = new Intl.Collator('zh');
    // FNV-1a(32 位):与桌面工具 pick_letter 对同一名字映射出同一字母,
    // 保证浏览器截图与桌面处理的截图字母一致
    function fnv1a(str) {
        const bytes = new TextEncoder().encode(str);
        let h = 2166136261;
        for (const b of bytes) { h ^= b; h = Math.imul(h, 16777619); }
        return h >>> 0;
    }
    // 首字母:中文按拼音锚点二分;ASCII 字母取大写;日文/韩文/数字/符号等一律
    // 哈希成 A–Z(原字符可辨识国籍/形态,起不到匿名效果)。emoji 是代理对,
    // 须按完整码点取首字符
    function pinyinInitial(name) {
        const t = (name || '').trim();
        const ch = [...t][0];
        if (!ch) return '?';
        if (/[a-z]/i.test(ch)) return ch.toUpperCase();
        if (/[\u4e00-\u9fff]/.test(ch)) {
            if (zhColl.compare(ch, PY_ANCHORS[0]) < 0) return 'A';
            let lo = 0, hi = PY_ANCHORS.length - 1, ans = 'A';
            while (lo <= hi) {
                const m = (lo + hi) >> 1;
                if (zhColl.compare(ch, PY_ANCHORS[m]) >= 0) { ans = PY_LETTERS[m]; lo = m + 1; }
                else hi = m - 1;
            }
            return ans;
        }
        return String.fromCharCode(65 + fnv1a(t) % 26);
    }

    const RE_SPACE = /space\.bilibili\.com\/(\d+)/;

    // 元素上的 space 链接地址（编辑态 href 被移存 data-anon-href，两种属性都认）
    function hrefOf(el) {
        return (el.getAttribute && (el.getAttribute('data-anon-href') || el.getAttribute('href'))) || '';
    }

    // 从元素父链提取 space 链接里的用户 mid（头像自身的链接也带）
    function spaceMidFrom(el) {
        let n = el;
        while (n) {
            if (n.getAttribute) {
                const m = RE_SPACE.exec(hrefOf(n));
                if (m) return m[1];
            }
            n = n.parentElement;
        }
        return null;
    }

    // 事件 composedPath 含沿途全部 shadow root：头像的 <a> 即使在别的层级也能找到 mid
    function midFromEvent(e) {
        const path = e.composedPath ? e.composedPath() : [];
        for (const el of path) {
            if (el.getAttribute) {
                const m = RE_SPACE.exec(hrefOf(el));
                if (m) return m[1];
            }
        }
        return null;
    }

    function cleanName(text) {
        return (text || '').trim().replace(/^@/, '');
    }

    // 收集 root 及其全部后代 shadow root：穿透 shadow 的遍历只此一处
    function deepRoots(root = document) {
        const out = [root];
        root.querySelectorAll('*').forEach(el => { if (el.shadowRoot) out.push(...deepRoots(el.shadowRoot)); });
        return out;
    }

    function deepQueryAll(selector, root = document) {
        const out = [];
        for (const r of deepRoots(root)) {
            try { r.querySelectorAll(selector).forEach(el => out.push(el)); } catch (err) { }
        }
        return out;
    }

    // 收集全页面（穿透所有 shadow root）的 space 链接。名字链接与头像链接 mid 一致，
    // 但二者常在不同 shadow 分支（如 bili-comment-user-info 的 shadowRoot），事件路径扫不全，须全页扫
    function deepSpaceLinks() {
        const out = [];
        for (const a of deepQueryAll('a[href], a[data-anon-href]')) {
            const m = RE_SPACE.exec(hrefOf(a));
            if (m) out.push({ mid: m[1], a: a });
        }
        return out;
    }

    // 元素是否属于评论区组件（bili-comments 之内）：用户资料悬浮卡挂在组件外、
    // 但里面的头像/名字链接指向同一个 space mid，按 mid 反查时须优先评论区本体
    function inComments(el) {
        let n = el;
        while (n) {
            if (n.tagName === 'BILI-COMMENTS') return true;
            const root = n.getRootNode ? n.getRootNode() : document;
            if (root === document) return false;
            n = root.host;
        }
        return false;
    }

    // 与 mid 一致的纯文本名字链接：按 mid 配对不会认错人；
    // 评论区内的优先（悬浮卡里同 mid 的名字链接只作兜底）
    function nameLinkByMid(mid) {
        if (!mid) return null;
        let fb = null;
        for (const { mid: m, a } of deepSpaceLinks()) {
            if (m !== mid) continue;
            if (a.querySelector('img, bili-avatar, [class*="avatar"]')) continue; // 头像链接
            if (!cleanName(a.textContent)) continue;
            if (inComments(a)) return a;
            if (!fb) fb = a;
        }
        return fb;
    }

    // 与 mid 一致的头像（点名字后按 mid 反查补盖）：bili-avatar host 优先（本体可能
    // 没有 img），退化取链接内 img；评论区内的优先，避免命中悬浮卡里的同一用户头像
    function avatarByMid(mid) {
        if (!mid) return null;
        let fb = null;
        for (const { mid: m, a } of deepSpaceLinks()) {
            if (m !== mid) continue;
            const inC = inComments(a);
            const av = a.querySelector('bili-avatar');
            if (av && avatarCoreOf(av)) {
                if (inC) return av;
                if (!fb) fb = av;
                continue;
            }
            const img = avatarImgOf(a);
            if (img) {
                if (inC) return img;
                if (!fb) fb = img;
            }
        }
        return fb;
    }

    // 在事件路径经过的各 shadow root 里找 ｢纯文本的 space 用户名链接｣（无 mid 时的兜底）
    function nameFromEventShadowRoots(e, excludeEl) {
        const path = e.composedPath ? e.composedPath() : [];
        for (const node of path) {
            if (node.nodeType !== 11 || !node.querySelectorAll) continue; // 11 = shadow root
            for (const a of node.querySelectorAll('a[data-anon-href], a[href*="space.bilibili.com"]')) {
                if (a === excludeEl || (excludeEl && a.contains(excludeEl))) continue;
                if (a.querySelector('img, bili-avatar, [class*="avatar"]')) continue; // 头像链接
                if (!cleanName(a.textContent)) continue;
                return a;
            }
        }
        return null;
    }

    // 穿透 shadow 边界的 closest：shadow 内元素 closest 到 shadowRoot 就断链，
    // 沿 host 向上继续（点击落在头像装扮层上时仍能命中 bili-avatar）
    function shadowClosest(el, sel) {
        let n = el;
        while (n) {
            if (n.matches && n.matches(sel)) return n;
            const root = n.getRootNode ? n.getRootNode() : document;
            if (root === document) return n.closest ? n.closest(sel) : null;
            n = root.host;
        }
        return null;
    }

    // 找本体头像 img：头像图 URL 在 /bfs/face/ 目录（装扮框图也在别的 img 里，
    // 且 DOM 序常在前，取第一个 img 会错换装扮图）；其次取 picture 内的，最后才兜底第一个
    function avatarImgOf(rootEl) {
        if (!rootEl) return null;
        if (rootEl.tagName === 'IMG') return rootEl;
        const imgs = deepQueryAll('img', rootEl);
        if (!imgs.length) return null;
        return imgs.find(i => /bfs\/face\//.test(i.currentSrc || i.src || ''))
            || imgs.find(i => i.parentElement && i.parentElement.tagName === 'PICTURE')
            || imgs[0];
    }

    // bili-avatar shadow 内的图层资源 URL（.layer-res 的背景图 + 层内 img）
    function layerUrl(layer) {
        const res = layer.querySelector('.layer-res');
        const m = res && /url\(["']?([^"')]+)/.exec(res.style.backgroundImage || '');
        const img = layer.querySelector('img');
        return ((m && m[1]) || '') + ' ' + ((img && (img.currentSrc || img.src)) || '');
    }

    // 定位 bili-avatar shadow 内的 ｢本体层｣。真实结构（0.4.8 dump 实测）：shadow 是若干
    // .layers 组，每组内若干 .layer.center；本体资源在 /bfs/face/ 且层带圆形裁剪，但两种
    // 形态并存——普通用户本体是层内 img，部分用户本体是 .layer-res 的 background，唯一的
    // img 反而是装扮框（URL 同在 /bfs/face/ 但层无圆形裁剪、尺寸大于本体）。只找 img 会
    // 错换装扮框。规则：圆形裁剪层中取 /bfs/face/ 命中者，再取尺寸最小（本体恒小于装扮框）
    function avatarCoreOf(host) {
        const sr = host && host.shadowRoot;
        if (!sr) return null;
        const layers = [...sr.querySelectorAll('.layer')];
        if (!layers.length) return null;
        let pool = layers.filter(l => /50%/.test(l.style.borderRadius || ''));
        if (!pool.length) pool = layers;
        const face = pool.filter(l => {
            const u = layerUrl(l);
            // 本体资源在 /bfs/face/；已打码态本体被换成 dataURL（背景或 img.currentSrc），
            // 同样视为本体候选，否则定位会漂移到尺寸更小的圆形挂件层、把真本体当装扮隐藏
            return /\/bfs\/face\//.test(u) || /data:image\//.test(u);
        });
        const pick = face.length ? face : pool;
        const core = pick.reduce((a, b) => (parseFloat(b.style.width) < parseFloat(a.style.width) ? b : a));
        return { core: core, img: core.querySelector('img') };
    }

    // 穿透 shadow 边界向上找评论容器（closest 不跨 shadow root）
    function scopeOf(el) {
        let n = el;
        while (n) {
            if (n.closest) {
                const s = n.closest('.reply-item, .reply-content, [class*="author"], [class*="comment"], article, .opus-module, [class*="user-info"]');
                if (s) return s;
            }
            const root = n.getRootNode ? n.getRootNode() : document;
            if (root === document) return null;
            n = root.host;
        }
        return null;
    }

    // 与桌面工具观感一致的纯色：金色角序列，白字可读
    const colorFor = (hue) => `hsl(${Math.round(hue)} 45% 36%)`;

    // 色相分配：以 uid 的黄金角序列为起点，与已有档案保持全局色距（≥36°，避免任何
    // 两用户撞色——黄金角序列任意两点可以很近），同字母用户进一步拉开（≥90°，
    // 同缩写必须一眼可辨）。约束冲突无解时（同字母太多挤在一起）取同字母距离
    // 最大的退化解
    function pickColor(uid, letter) {
        const used = Object.values(users);
        const dist = (h, u) => (u.hue === undefined || u.hue === null) ? 999 :
            Math.min(Math.abs(h - u.hue), 360 - Math.abs(h - u.hue));
        let okAny = null, okSameD = -1, bestAny = null, bestScore = -1;
        for (let i = 0; i < 720; i++) {
            const cand = (uid * 137.508 + i * 137.508) % 360;
            let dMin = 999, dSame = 999;
            for (const u of used) {
                const d = dist(cand, u);
                dMin = Math.min(dMin, d);
                if (u.letter === letter) dSame = Math.min(dSame, d);
            }
            if (dMin >= 36 && dSame >= 90) return cand;
            if (dMin >= 30 && dSame > okSameD) { okAny = cand; okSameD = dSame; }
            const score = Math.min(dMin, 36) / 36 + Math.min(dSame, 90) / 90;
            if (score > bestScore) { bestScore = score; bestAny = cand; }
        }
        return okAny !== null ? okAny : bestAny;
    }

    // 建/取档案：同一 mid 恒返回同一档案；名字首次确认时固化并算出拼音首字母。
    // 颜色建档时按 pickColor 分配并存 hue（同字母用户色相拉开）。手动打码视为
    // 重新启用该用户（清除撤销时打的 skip 标记）
    function infoFor(mid, name) {
        const key = mid ? 'mid:' + mid : 'name:' + (name || '');
        let info = users[key];
        if (!info) {
            const uid = Math.max(0, ...Object.values(users).map(u => u.uid || 0)) + 1;
            const letter = name ? pinyinInitial(name) : '?';
            const hue = pickColor(uid, letter);
            info = { uid: uid, color: colorFor(hue), hue: hue, letter: letter, name: name || '' };
            users[key] = info;
        } else if (name && !info.name) {
            info.name = name;
            info.letter = pinyinInitial(name);
        }
        info.skip = false;
        return info;
    }

    function userByUid(uid) {
        for (const u of Object.values(users)) if (u.uid === uid) return u;
        return null;
    }

    // 手动撤销某用户后不再对其自动重打（否则 MutationObserver 600ms 后 applyKnown
    // 会把刚撤销的名字/头像按已知名单重新盖上，表现为 ｢闪一下恢复又被盖住｣）；
    // ｢应用已知名单｣按钮不受此限
    function markSkip(uid) {
        const u = userByUid(typeof uid === 'number' ? uid : parseInt(uid, 10));
        if (u) u.skip = true;
    }

    // 页面上是否还有打码元素引用该 uid（名字 data-anon-uid / 头像 data-anon-info）
    function pageHasUserRefs(uid) {
        const probe = (el) => parseInt(el.dataset.anonUid || el.dataset.anonInfo, 10) === uid;
        return deepQueryAll('[data-anon-uid], [data-anon-info]').some(probe);
    }

    // 撤销后处理档案：先打 skip 挡自动重打；页面已无该用户的打码点时连档案一起
    // 删除（编号/颜色释放，重新打码按新档案分配）
    let clearing = false; // 全页撤销置位：users 随后整体清空，逐个释放档案是白做

    function releaseUser(marker) {
        if (clearing) return;
        const uid = parseInt(marker, 10);
        const u = userByUid(uid);
        if (!u) return;
        markSkip(uid);
        if (pageHasUserRefs(uid)) return;
        for (const [k, v] of Object.entries(users)) {
            if (v === u) { delete users[k]; break; }
        }
    }

    // ---------------- 打码实现 ----------------
    // 纯色圆 + 字母画成 PNG：直接替换头像 <img> 的 src，大小天然与头像元素一致
    //（几何测量在装扮层/shadow 布局下不可靠，改图不受影响）；撤销时换回原 src
    function avatarDataURL(info, size = 128) {
        const c = document.createElement('canvas');
        c.width = c.height = size;
        const ctx = c.getContext('2d');
        ctx.fillStyle = info ? info.color : '#555';
        ctx.beginPath();
        ctx.arc(size / 2, size / 2, size / 2, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = '#fff';
        ctx.font = `700 ${Math.round(size * 0.45)}px sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(info ? info.letter : '?', size / 2, size / 2 + size * 0.03);
        return c.toDataURL('image/png');
    }

    // 裸 img 形态的打码（无 bili-avatar shadow 的旧结构兜底；子回复楼层也是此形态）
    function maskAvatarImg(img, info) {
        if (!img) return false;
        if (img.dataset.anonDone) {
            // 已盖：另一条点击路径后拿到名字时刷新字母/颜色
            if (info && info.name) img.src = avatarDataURL(info);
            return false;
        }
        img.dataset.anonSrc = img.src;
        img.src = avatarDataURL(info);
        img.dataset.anonInfo = info ? (info.uid + '|' + info.letter) : '';
        img.dataset.anonDone = '1';
        guardAvatar(img);
        return true;
    }

    function unmaskAvatarImg(img) {
        if (!img || !img.dataset.anonDone) return;
        const marker = img.dataset.anonInfo;
        if (img.dataset.anonSrc) img.src = img.dataset.anonSrc;
        delete img.dataset.anonSrc;
        delete img.dataset.anonInfo;
        delete img.dataset.anonDone;
        guardedAvatars.delete(img);
        releaseUser(marker);
    }

    // 隐藏头像装扮：bili-avatar shadow 内凡与本体层无祖先/子孙关系的元素都是装扮框/
    // 挂件（本体可能是 background 没有 img，不能按 ｢是否含 img｣ 判定），全部隐藏；
    // 不按类名列举，结构升级也能覆盖
    function hideAvatarDecor(core) {
        const root = core.getRootNode();
        if (!root || root === document) return;
        root.querySelectorAll('*').forEach(el => {
            if (el.tagName === 'STYLE' || el.contains(core) || core.contains(el)) return;
            if (!el.dataset.anonShown) {
                el.dataset.anonShown = '1';
                el.style.display = 'none';
            }
        });
    }

    function restoreAvatarDecor(core) {
        const root = core.getRootNode();
        if (!root || root === document) return;
        root.querySelectorAll('[data-anon-shown]').forEach(el => {
            el.style.display = '';
            delete el.dataset.anonShown;
        });
    }

    // 把本体换成打码图。img 型必须连同所在 <picture> 的全部 <source srcset> 一起替换：
    // source 的 srcset 候选优先于 img.src，只改 src 时浏览器 currentSrc 仍是原图，显示不变
    function setAvatarUrl(c, url) {
        if (c.img) {
            const pic = c.img.closest('picture');
            if (pic) pic.querySelectorAll('source').forEach(s => { s.srcset = url; });
            c.img.src = url;
        } else {
            const res = c.core.querySelector('.layer-res');
            res.style.cssText = `background: center center / cover no-repeat url("${url}");`;
        }
    }

    function saveAvatarUrl(host, c) {
        if (c.img) {
            host.dataset.anonSrc = c.img.src;
            const pic = c.img.closest('picture');
            if (pic) host.dataset.anonSrcset = [...pic.querySelectorAll('source')].map(s => s.srcset).join('\n');
        } else {
            const res = c.core.querySelector('.layer-res');
            host.dataset.anonBg = res.getAttribute('style') || '';
        }
    }

    function restoreAvatarUrl(host, c) {
        if (c.img) {
            if (host.dataset.anonSrc) c.img.src = host.dataset.anonSrc;
            const pic = c.img.closest('picture');
            if (pic && host.dataset.anonSrcset !== undefined) {
                const sets = host.dataset.anonSrcset.split('\n');
                pic.querySelectorAll('source').forEach((s, i) => { if (sets[i] !== undefined) s.srcset = sets[i]; });
            }
        } else {
            const res = c.core.querySelector('.layer-res');
            if (res && host.dataset.anonBg !== undefined) res.setAttribute('style', host.dataset.anonBg);
        }
    }

    // bili-avatar host 形态的打码：本体是 img 则换 src，是 .layer-res 背景则换 background；
    // 打码标记记在 host 上（本体可能没有 img）
    function maskAvatarHost(host, info) {
        const c = avatarCoreOf(host);
        if (!c) return false;
        if (host.dataset.anonDone) {
            // 已盖：另一条点击路径后拿到名字时刷新字母/颜色
            if (info && info.name) setAvatarUrl(c, avatarDataURL(info));
            hideAvatarDecor(c.core);
            return false;
        }
        saveAvatarUrl(host, c);
        setAvatarUrl(c, avatarDataURL(info));
        host.dataset.anonInfo = info ? (info.uid + '|' + info.letter) : '';
        host.dataset.anonDone = '1';
        hideAvatarDecor(c.core);
        guardAvatar(host);
        return true;
    }

    function unmaskAvatarHost(host) {
        if (!host || !host.dataset.anonDone) return;
        const marker = host.dataset.anonInfo;
        const c = avatarCoreOf(host);
        if (c) {
            restoreAvatarUrl(host, c);
            restoreAvatarDecor(c.core);
        }
        delete host.dataset.anonSrc;
        delete host.dataset.anonSrcset;
        delete host.dataset.anonBg;
        delete host.dataset.anonInfo;
        delete host.dataset.anonDone;
        guardedAvatars.delete(host);
        releaseUser(marker);
    }

    // 统一打码/撤销入口：bili-avatar host（含本体为 background 的情况）优先，裸 img 退化；
    // 带原名标记的名字元素走名字撤销
    function maskAvatarAny(node, info) {
        if (!node) return false;
        if (node.tagName === 'BILI-AVATAR' || avatarCoreOf(node)) return maskAvatarHost(node, info);
        return maskAvatarImg(node, info);
    }

    function unmaskAny(node) {
        if (!node) return;
        if (node.dataset && node.dataset.anonOrig !== undefined) return unmaskName(node);
        if (node.tagName === 'BILI-AVATAR' || avatarCoreOf(node)) return unmaskAvatarHost(node);
        return unmaskAvatarImg(node);
    }

    // ---------------- 防还原守卫 ----------------
    // 评论组件的异步重渲染（点赞数/时间等数据到达触发 lit 重渲染）会把 shadow 内
    // 头像资源按组件状态重写，覆盖掉打码图（懒加载新楼层尤其频繁）。打码过的头像
    // 登记在案，监听 src/srcset/style 变化，发现被还原为原图就重设打码图
    const guardedAvatars = new Set();
    let guardTimer = null;
    const avGuard = new MutationObserver(() => {
        clearTimeout(guardTimer);
        guardTimer = setTimeout(verifyGuarded, 150);
    });

    function guardAvatar(el) {
        guardedAvatars.add(el);
        avGuard.observe(el.getRootNode() === document ? document.body : el.getRootNode(),
            { attributes: true, attributeFilter: ['src', 'srcset', 'style'], childList: true, subtree: true });
    }

    function verifyGuarded() {
        for (const el of [...guardedAvatars]) {
            if (!el.isConnected || !el.dataset.anonDone) { guardedAvatars.delete(el); continue; }
            const info = userByUid(parseInt(el.dataset.anonInfo, 10));
            if (!info) { guardedAvatars.delete(el); continue; }
            const url = avatarDataURL(info);
            if (el.tagName === 'IMG') {
                if (!/data:image/.test(el.getAttribute('src') || '')) el.src = url;
                continue;
            }
            const c = avatarCoreOf(el);
            if (!c) continue;
            const res = c.core.querySelector('.layer-res');
            const imgBad = c.img && !/data:image/.test(c.img.getAttribute('src') || '');
            const bgBad = !c.img && res && !/data:image/.test(res.getAttribute('style') || '');
            if (imgBad || bgBad) setAvatarUrl(c, url);
        }
    }

    // ｢数字周边｣卡（bili-comment-user-sailing-card，粉丝编号牌）：沿 shadow host 链找到
    // 所在评论组件后整卡隐藏（卡在 renderer 自己的 shadowRoot 里）
    function sailingCardOf(el) {
        let n = el;
        while (n) {
            const root = n.getRootNode ? n.getRootNode() : document;
            if (root === document) return null;
            const host = root.host;
            if (host && host.tagName === 'BILI-COMMENT-RENDERER' && host.shadowRoot) {
                return host.shadowRoot.querySelector('bili-comment-user-sailing-card');
            }
            n = host;
        }
        return null;
    }

    function hideSailingCard(el) {
        const card = sailingCardOf(el);
        if (card && !card.dataset.anonShown) {
            card.dataset.anonShown = '1';
            card.style.display = 'none';
        }
    }

    function restoreSailingCard(el) {
        const card = sailingCardOf(el);
        if (card && card.dataset.anonShown) {
            card.style.display = '';
            delete card.dataset.anonShown;
        }
    }

    // 用户名：textContent 替换为 ｢用户+拼音首字母｣（与头像圆内字母一致；@提及保留 @），
    // 原名存 dataset 可恢复；内部 uid 记录在 dataset 供撤销时定位档案
    function maskNameEl(el, info) {
        const orig = el.dataset.anonOrig || el.textContent.trim();
        if (!orig || orig.length > 40) return false;
        const isAt = orig.startsWith('@');
        el.dataset.anonOrig = orig;
        el.dataset.anonUid = String(info.uid);
        el.dataset.anonDone = '1';
        el.textContent = (isAt ? '@' : '') + '用户' + info.letter;
        el.style.color = info.color;
        el.style.fontWeight = '600';
        el.title = '';
        return true;
    }

    function unmaskName(el) {
        const marker = el.dataset.anonUid;
        el.textContent = el.dataset.anonOrig || el.textContent;
        delete el.dataset.anonOrig;
        delete el.dataset.anonUid;
        delete el.dataset.anonDone;
        el.style.color = '';
        el.style.fontWeight = '';
        restoreSailingCard(el);
        releaseUser(marker);
    }

    // 名字元素所在楼层的头像：名字与头像分处评论组件的不同 shadow 分支（closest 找不到），
    // 沿 host 链找到所在楼层的评论组件后在其 shadow 里找头像。主楼头像是 bili-avatar；
    // 子回复楼层不用该组件，头像就是链接里的裸 img（/bfs/face/），一并兜住
    function avatarInSameComment(el) {
        let n = el;
        while (n) {
            const root = n.getRootNode ? n.getRootNode() : document;
            if (root === document) break;
            const host = root.host;
            if (host && /^BILI-COMMENT-(REPLY-)?RENDERER$/.test(host.tagName) && host.shadowRoot) {
                const sr = host.shadowRoot;
                const av = sr.querySelector('bili-avatar');
                if (av && avatarCoreOf(av)) return av;
                // 编辑态下链接 href 已被移除存进 data-anon-href，两种属性都要匹配；
                // 已打码态本体是 dataURL，同样要能认出（二次查找/撤销定位）
                const img = [...sr.querySelectorAll('a[data-anon-href] img, a[href*="space.bilibili.com"] img')]
                    .find(i => /(bfs\/face\/|data:image\/)/.test(i.currentSrc || i.src || ''));
                if (img) return img;
            }
            n = host;
        }
        return null;
    }

    // 名字元素附近的头像：bili-avatar host 优先（本体可能是 background），裸 img 退化
    function avatarNear(el) {
        const scope = el.closest('.reply-item, .reply-content, [class*="author"], [class*="comment"], article, .opus-module') || el.parentElement;
        if (!scope) return null;
        const av = scope.querySelector('bili-avatar');
        if (av && avatarCoreOf(av)) return av;
        const direct = scope.querySelector('.bili-avatar img, [class*="avatar"] img, img[src*="face"]');
        if (direct) return direct;
        const box = scope.querySelector('.bili-avatar, [class*="avatar"]');
        return box ? (avatarCoreOf(box) ? box : avatarImgOf(box)) : null;
    }

    // 名字文本该配谁的头像：只有楼层主人名（user-info 之内）才配本层头像；正文里的
    // @提及/引用挂在别人楼层（本层头像属于楼层主人），配了会把排除名单用户的头像
    // 盖成被提及者的码，只打文本；评论区之外（作者区等）仍就近找头像
    function avatarForName(el) {
        let n = el;
        while (n) {
            const root = n.getRootNode ? n.getRootNode() : document;
            if (root === document) break;
            const host = root.host;
            if (host) {
                if (host.tagName === 'BILI-COMMENT-USER-INFO') return avatarInSameComment(el);
                if (host.tagName === 'BILI-COMMENTS') return null;
            }
            n = host;
        }
        return avatarNear(el);
    }

    // 头像附近的用户名链接（同 shadow root 内优先，再沿容器边界找；排除头像自身的链接）
    function nameNearAvatar(imgEl) {
        const roots = [imgEl.getRootNode()];
        const scope = scopeOf(imgEl);
        if (scope) roots.push(scope);
        for (const root of roots) {
            if (!root || !root.querySelectorAll) continue;
            for (const a of root.querySelectorAll('a[data-anon-href], a[href*="space.bilibili.com"]')) {
                if (!a.contains(imgEl) && !a.querySelector('img') && cleanName(a.textContent)) return a;
            }
        }
        return null;
    }

    // 头像与用户名同包在一个链接里时，链接文本即用户名
    function nameFromAvatarLink(imgEl) {
        const a = imgEl.closest && imgEl.closest('a[data-anon-href], a[href*="space.bilibili.com"]');
        return (a && cleanName(a.textContent)) ? a : null;
    }

    // 部分版本评论区用户名不带 space 链接：在头像所在 shadow root / 评论容器内
    // 按 class 含 name 找短文本元素（与头像同容器约束保证不会认到别人）
    function nameElNearAvatar(imgEl) {
        const roots = [imgEl.getRootNode()];
        const scope = scopeOf(imgEl);
        if (scope) roots.push(scope);
        for (const root of roots) {
            if (!root || !root.querySelectorAll) continue;
            for (const el of root.querySelectorAll('[class*="name"]')) {
                if (el.contains(imgEl) || el.querySelector('img')) continue;
                const t = cleanName(el.textContent);
                if (t && t.length <= 40) return el;
            }
        }
        return null;
    }

    // 名字元素 + 本楼层头像成套打码：各自判断是否已处理（懒加载漏盖的一方补上），
    // 任一新盖即顺带隐藏数字周边卡。返回是否新盖了任何东西
    function maskUserAt(nameEl, av, info) {
        const nameOk = nameEl && !nameEl.dataset.anonDone ? maskNameEl(nameEl, info) : false;
        const avOk = av ? maskAvatarAny(av, info) : false;
        if (nameOk || avOk) hideSailingCard(nameEl || av);
        return nameOk || avOk;
    }

    function maskNameInteractive(el) {
        const info = infoFor(spaceMidFrom(el), cleanName(el.textContent));
        if (!info.name) return;
        if (maskNameEl(el, info)) {
            const av = avatarNear(el);
            if (av) maskAvatarAny(av, info);
            hideSailingCard(el);
        }
    }

    // 编辑态点击 → 识别头像/名字
    const NAME_SEL = '.user-name, .up-name, .jump-link-user-name, .opus-module-author__name, [class*="user-name"], [class*="nickname"]';

    // 覆盖层穿透：点击命中悬浮卡片等覆盖层时，临时隐藏点击路径上的顶层主文档宿主
    // （卡片挂载点），对同一坐标重新 hit-test 命中其下方的名字/头像；
    // 确认被覆盖层遮挡时保持该宿主隐藏（即关掉悬浮卡片），否则立即恢复
    function hitWithCoverBypass(e, t) {
        const direct = targetFrom(t);
        const path = e.composedPath ? e.composedPath() : [];
        const topHost = path.find(el => el instanceof Element && el.getRootNode() === document);
        let bypass = null;
        if (topHost) {
            const prev = topHost.style.display;
            topHost.style.display = 'none';
            try {
                const stack = document.elementsFromPoint(e.clientX, e.clientY);
                for (const el of stack) {
                    const h = targetFrom(el);
                    if (h) { bypass = h; break; }
                }
            } finally {
                topHost.style.display = bypass ? 'none' : prev;
            }
        }
        if (bypass) return bypass;
        return direct;
    }

    function onEditClick(e, t) {
        // 撤销优先：头像 host 上的打码标记（本体可能是 background 没有独立的 img），
        // 名字查改文本标记
        const tImg = targetFrom(t);
        if (tImg && (tImg.kind === 'avatar' || tImg.kind === 'img') && tImg.el.dataset.anonDone) {
            unmaskAny(tImg.el);
            setStatus('已撤销头像打码（映射保留）');
            return;
        }
        const done = t.closest('[data-anon-done]');
        if (done && done.dataset.anonOrig !== undefined) {
            unmaskName(done);
            setStatus('已撤销名字打码（映射保留）');
            return;
        }
        // 识别：space 链接（含头像 = 头像链接，纯文本 = 名字链接）或裸头像图；
        // 被悬浮卡片等覆盖层遮挡时穿透重取
        const hit = hitWithCoverBypass(e, t);
        if (hit) {
            const mid = midFromEvent(e) || spaceMidFrom(hit.el);
            if (hit.kind === 'avatar' || hit.kind === 'img') {
                // 取名：按 mid 配对的名字链接 → 同容器纯文本链接 → 同容器 class 含 name 的元素
                // → 头像所在链接整体文本（部分版本评论区用户名不带链接，靠容器约束不认错人）
                const nameA = (mid && nameLinkByMid(mid)) || nameNearAvatar(hit.el) || nameElNearAvatar(hit.el) || nameFromAvatarLink(hit.el) || (!mid && nameFromEventShadowRoots(e, hit.el));
                const name = nameA ? cleanName(nameA.textContent) : '';
                const info = infoFor(mid, name);
                maskUserAt(nameA, hit.el, info);
                hideHoverCardsSoon();
                setStatus(info.name
                    ? `已打码：${info.name} → 用户${info.letter}（头像+名字）`
                    : `已盖头像圆（mid:${mid || '?'}，本页暂未找到名字文本）`);
                return;
            }
            // 名字链接：同一 mid 即同一档案，头像按所在楼层优先、再按 mid 反查补盖
            const name = cleanName(hit.el.textContent);
            const info = infoFor(mid, name);
            if (info.name || mid) {
                maskUserAt(hit.el, avatarForName(hit.el) || avatarByMid(mid), info);
                hideHoverCardsSoon();
                setStatus(`已打码：${info.name || name || 'mid:' + mid} → 用户${info.letter}（头像+名字）`);
                return;
            }
        }
        // 非链接的昵称类元素兜底
        const nEl = t.closest(NAME_SEL);
        if (nEl) { maskNameInteractive(nEl); return; }
        // 诊断输出：shadow 内真实目标的标签/类名
        const fmt = (el) => el.tagName + (el.className ? '.' + String(el.className).split(' ')[0].slice(0, 28) : '');
        setStatus('未识别: target=' + fmt(t) + (t.id ? '#' + String(t.id).slice(0, 16) : ''));
    }

    // B 站所有用户相关元素（头像/用户名/@提及）都是指向 space.bilibili.com 的链接；
    // 用 href 识别比类名鲁棒：含头像 = 头像链接，纯文本 = 名字链接。

    function targetFrom(t) {
        const a = shadowClosest(t, 'a[data-anon-href], a[href*="space.bilibili.com"]');
        if (a) {
            // 本体在 bili-avatar shadow 内（可能是 background 没有 img），命中时以 host 为准
            const av = a.querySelector('bili-avatar');
            if (av && avatarCoreOf(av)) return { kind: 'avatar', el: av };
            const img = avatarImgOf(a);
            return img ? { kind: 'img', el: img } : { kind: 'a', el: a };
        }
        // 新版头像组件：可点击头像不包在链接里，而是 bili-avatar 等自带 shadow 的元素；
        // closest 不跨 shadowRoot，点在装扮层上时须沿 host 链找到 bili-avatar
        const box = shadowClosest(t, 'bili-avatar, [class*="avatar"]');
        if (box) {
            if (avatarCoreOf(box)) return { kind: 'avatar', el: box.tagName === 'BILI-AVATAR' ? box : (box.querySelector('bili-avatar') || box) };
            const img = avatarImgOf(box);
            if (img) return { kind: 'img', el: img };
        }
        if (t.tagName === 'IMG') {
            const ctx = (t.className || '') + ' ' + (t.parentElement?.className || '') + ' ' + (t.src || '');
            if (/50%/.test(getComputedStyle(t).borderRadius) || /avatar|face/i.test(ctx)) {
                return { kind: 'img', el: t };
            }
        }
        return null;
    }

    // 一键打码本页全部评论用户：按楼层组件遍历，每层从 USER-INFO 取名字行、
    // avatarInSameComment 取本层头像，成套处理；翻页后对新增楼层再点一次即可。
    // 名字与头像各自判断是否已处理，已打码楼层漏掉的头像（如懒加载晚于首次点击）
    // 会在再次点击时补齐。排除名单取工具条输入框（逗号/空格分隔）
    function maskAllComments() {
        const exc = (bar.querySelector('#anon-exclude')?.value || '')
            .split(/[,，;；\s]+/).map(s => s.trim()).filter(Boolean);
        let n = 0;
        for (const rr of deepQueryAll('bili-comment-renderer, bili-comment-reply-renderer')) {
            if (!rr.shadowRoot) continue;
            const ui = rr.shadowRoot.querySelector('bili-comment-user-info');
            if (!ui || !ui.shadowRoot) continue;
            const nameEl = [...ui.shadowRoot.querySelectorAll('a[data-anon-href], a[href*="space.bilibili.com"]')]
                .find(a => !a.querySelector('img, bili-avatar') && cleanName(a.dataset.anonOrig || a.textContent));
            if (!nameEl) continue;
            const orig = cleanName(nameEl.dataset.anonOrig || nameEl.textContent);
            if (!orig || exc.includes(orig)) continue;
            const info = infoFor(spaceMidFrom(nameEl), orig);
            if (exc.includes(info.name)) continue;
            if (maskUserAt(nameEl, avatarInSameComment(nameEl), info)) n++;
        }
        return n;
    }

    // ---------------- 已知名单应用（穿透 shadow 全文精确匹配） ----------------
    function* deepTextNodes() {
        for (const r of deepRoots()) {
            const w = document.createTreeWalker(r, NodeFilter.SHOW_TEXT);
            while (w.nextNode()) yield w.currentNode;
        }
    }

    // force=true（工具条按钮）时忽略 skip 标记强制全应用；自动路径尊重手动撤销的 skip
    function applyKnown(force) {
        const byName = new Map();
        for (const u of Object.values(users)) {
            if (u.name && (force || !u.skip)) byName.set(u.name, u);
        }
        if (!byName.size) return 0;
        let n = 0;
        const hits = [];
        for (const node of deepTextNodes()) {
            const txt = node.textContent.trim();
            if (!txt) continue;
            const bare = txt.startsWith('@') ? txt.slice(1) : txt;
            const info = byName.get(bare);
            if (info) hits.push([node, info]);
        }
        for (const [node, info] of hits) {
            const el = node.parentElement;
            if (!el || el.dataset.anonDone) continue;
            if (maskUserAt(el, avatarForName(el), info)) n++;
        }
        return n;
    }

    // 自动应用已知名单 + 编辑态下对新插入内容禁 href（评论区懒加载/展开回复）。
    // 防抖 600ms，但播放中的视频页 body 常态变动间隙 <600ms（实测 <270ms），
    // 纯防抖会被无限重置饿死，故每轮变动起点起最多 2s 强制执行一次
    let moTimer = null;
    let moFirst = 0;
    const mo = new MutationObserver(() => {
        if (!moTimer) moFirst = Date.now();
        clearTimeout(moTimer);
        moTimer = setTimeout(() => {
            moTimer = null;
            if (editing) disableLinks();
            if (autoApply) applyKnown();
        }, Math.min(600, Math.max(0, moFirst + 2000 - Date.now())));
    });

    // ---------------- UI ----------------
    let editing = false;
    const css = document.createElement('style');
    css.textContent = `
#anon-btn,#anon-bar{position:fixed;left:16px;z-index:2147483647;font-family:sans-serif}
#anon-btn{bottom:16px;width:44px;height:44px;border-radius:50%;background:#fb7299;color:#fff;
 display:flex;align-items:center;justify-content:center;font-size:20px;cursor:pointer;
 box-shadow:0 2px 8px rgba(0,0,0,.3);user-select:none}
#anon-btn.editing{background:#333}
#anon-bar{bottom:70px;background:#fff;border:1px solid #e3e5e7;border-radius:8px;padding:8px;
 display:none;flex-direction:column;gap:6px;box-shadow:0 2px 12px rgba(0,0,0,.15);font-size:13px}
#anon-bar.show{display:flex}
#anon-bar .anon-tip{color:#666;max-width:260px;line-height:1.5}
#anon-bar button{border:1px solid #e3e5e7;background:#f6f7f8;border-radius:6px;padding:5px 8px;
 cursor:pointer;text-align:left}
#anon-bar button:hover{background:#fb7299;color:#fff}
#anon-bar input{border:1px solid #e3e5e7;border-radius:6px;padding:5px 8px;font-size:13px;box-sizing:border-box}
body.anon-editing [data-anon-done]{outline:1px dashed #fb7299}
`;
    document.documentElement.appendChild(css);

    const btn = document.createElement('div');
    btn.id = 'anon-btn';
    btn.textContent = '码';
    btn.title = '打码编辑态（Esc 退出）';
    document.body.appendChild(btn);

    const bar = document.createElement('div');
    bar.id = 'anon-bar';
    bar.innerHTML = `<div class="anon-tip">编辑态已禁用页面一切跳转与点击动作；翻页等操作请先退出（Esc/Alt+M）。点击头像=盖圆；点击用户名/@提及=替换为 ｢用户首字母｣（自动补盖旁边头像）；点已打码元素=撤销（无其他打码点时清除其映射）</div>
<input id="anon-exclude" placeholder="排除名单，逗号或空格分隔" value="MAA-Official">
<button data-a="all">全部打码（按上方排除名单跳过）</button>
<button data-a="apply">应用已知名单（本页全部自动打码）</button>
<button data-a="auto">自动应用：开</button>
<button data-a="export">导出映射（桌面工具格式，复制到剪贴板）</button>
<button data-a="clear">撤销本页全部打码</button>`;
    document.body.appendChild(bar);

    const setStatus = (s) => {
        const tip = bar.querySelector('.anon-tip');
        if (tip) tip.textContent = s;
    };

    // 编辑态下彻底禁跳转：穿透 shadow root 移除全部链接的 href（退出时恢复）。
    // B 站新版评论区是 shadow DOM 组件（bili-comments），主文档 querySelectorAll 看不到内部链接
    function disableLinks() {
        deepQueryAll('a[href]').forEach(a => {
            if (a.closest('#anon-bar, #anon-btn')) return;
            a.dataset.anonHref = a.getAttribute('href');
            a.removeAttribute('href');
        });
    }

    function restoreLinks() {
        deepQueryAll('a[data-anon-href]').forEach(a => {
            a.setAttribute('href', a.dataset.anonHref);
            delete a.dataset.anonHref;
        });
    }

    function setEditing(on) {
        editing = on;
        btn.classList.toggle('editing', on);
        bar.classList.toggle('show', on);
        document.body.classList.toggle('anon-editing', on);
        btn.title = on ? '退出打码编辑态（Esc/Alt+M）' : '打码编辑态（Alt+M 进入）';
        if (on) { disableLinks(); hideHoverCards(); } else restoreLinks();
    }

    btn.addEventListener('click', () => setEditing(!editing));

    bar.addEventListener('click', (e) => {
        const a = e.target.dataset && e.target.dataset.a;
        if (!a) return;
        if (a === 'all') {
            const n = maskAllComments();
            setStatus(`已打码本页 ${n} 位用户（排除名单外）；翻页后再点一次即可`);
        } else if (a === 'apply') {
            const n = applyKnown(true); // 按钮强制应用，手动撤销的 skip 不拦截
            setStatus(`已应用 ${n} 处已知名单`);
        } else if (a === 'auto') {
            autoApply = !autoApply;
            bar.querySelector('[data-a="auto"]').textContent = '自动应用：' + (autoApply ? '开' : '关');
        } else if (a === 'export') {
            const out = { users: {} };
            for (const [key, u] of Object.entries(users)) {
                out.users[u.name || key] = {
                    uid: u.uid, letter: u.letter,
                    rgb: hexToRgb(u.color),
                    hue: u.hue === undefined || u.hue === null ? null : Math.round(u.hue * 10) / 10
                };
            }
            navigator.clipboard.writeText(JSON.stringify(out, null, 1))
                .then(() => setStatus(`已导出 ${Object.keys(out.users).length} 个用户到剪贴板`))
                .catch(() => setStatus('导出失败：剪贴板不可用'));
        } else if (a === 'clear') {
            clearPage();
        }
    });

    function hexToRgb(cssColor) {
        const c = document.createElement('canvas').getContext('2d');
        c.fillStyle = '#000';
        c.fillStyle = cssColor;
        const m = c.fillStyle; // 归一化后为 #rrggbb
        if (m.startsWith('#')) {
            const v = parseInt(m.slice(1), 16);
            return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
        }
        return [85, 85, 85];
    }

    function clearPage() {
        clearing = true;
        try {
            deepQueryAll('[data-anon-done]').forEach(el => {
                if (el.tagName === 'IMG') unmaskAvatarImg(el);
                else if (el.tagName === 'BILI-AVATAR') unmaskAvatarHost(el);
                else unmaskName(el);
            });
            // 恢复被隐藏的数字周边卡（个别打码点已随 unmask 恢复，此处兜底全部）
            deepQueryAll('[data-anon-shown]').forEach(el => {
                el.style.display = '';
                delete el.dataset.anonShown;
            });
        } finally {
            clearing = false;
        }
        // 撤销=彻底清除：全部映射（编号/颜色）一并释放，重新打码按新档案分配
        for (const k of Object.keys(users)) delete users[k];
        setStatus('已撤销本页全部打码并清除映射');
    }

    // 编辑态：拦截页面一切点击/中键（工具条除外），杜绝任何页面动作；翻页等须先退出编辑态。
    // 同时拦 hover 事件：B 站的用户资料悬浮卡片由 hover 触发，会盖在名字/头像上方吃掉点击。
    // shadow DOM 事件在主文档被 retarget，用 composedPath()[0] 拿 shadow 内真实目标
    const realTarget = (e) => {
        const p = e.composedPath ? e.composedPath() : null;
        return (p && p.length) ? p[0] : e.target;
    };
    const swallow = (e) => {
        if (!editing) return;
        const t = realTarget(e);
        if (t.closest && t.closest('#anon-bar, #anon-btn')) return;
        e.preventDefault();
        e.stopImmediatePropagation();
        e.stopPropagation();
        if (e.type === 'click') onEditClick(e, t);
    };
    document.addEventListener('click', swallow, true);
    document.addEventListener('auxclick', swallow, true);
    ['mouseover', 'mouseenter', 'pointerover', 'pointerenter', 'mousemove'].forEach(ev =>
        document.addEventListener(ev, swallow, true));

    // 进入编辑态时隐藏已弹出的悬浮卡片（类名匹配不到也无害，hover 拦截会阻止新卡片）
    // 进入编辑态/每次打码后清理悬浮卡片：B 站卡片挂在 body 下、fixed 定位、
    // 文本含 ｢关注/发消息｣ 且很短（区别于内容超长的评论区容器）
    function hideHoverCards() {
        deepQueryAll(
            '[class*="user-card"], [class*="userCard"], [class*="bcc-user"], [class*="hover-card"]'
        ).forEach(el => { el.style.display = 'none'; });
        for (const el of document.body.children) {
            if (el.id === 'anon-btn' || el.id === 'anon-bar') continue;
            const cs = getComputedStyle(el);
            if ((cs.position === 'fixed' || cs.position === 'absolute') &&
                el.textContent.includes('发消息') && el.textContent.length < 400) {
                el.style.display = 'none';
            }
        }
    }

    // 打码动作会诱发 B 站异步弹出悬浮卡片：立即清一次，稍后再清一次
    function hideHoverCardsSoon() {
        hideHoverCards();
        setTimeout(hideHoverCards, 300);
    }

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && editing) setEditing(false);
        // Alt+M：键盘切换编辑态（左下角按钮的等价入口）
        if (e.altKey && !e.ctrlKey && !e.shiftKey && (e.key === 'm' || e.key === 'M')) {
            e.preventDefault();
            setEditing(!editing);
        }
    });

    mo.observe(document.body, { childList: true, subtree: true });
    setTimeout(applyKnown, 1500); // 首次进入页面自动应用已知名单
})();
