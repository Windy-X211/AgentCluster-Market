/*!
 * visual_interaction renderer
 * 可视化交互拓展 —— 注册 7 类渲染器：choice / fill / image / flowchart / mindmap / html / layout
 *
 * 运行环境：浏览器主世界。可访问：
 *   window.Vue               —— Vue 3 运行时（h, defineComponent, ref, reactive, computed, onMounted, onUnmounted）
 *   window.__registerVizRenderer__(type, component)
 *   window.__extAPI__        —— { sendUserInput, insertInput, imageURL, fetchAPI, openLightbox, getSettings }
 *
 * 每个渲染器是 Vue 组件，props: { data, raw, extAPI }
 * choice / fill 统一「提交按钮」模式：先作答 → 点提交发送 → 锁定
 */
(function () {
    const Vue = window.Vue;
    if (!Vue || !Vue.h || !Vue.defineComponent) {
        console.error('[visual_interaction] window.Vue 未就绪，渲染器未注册');
        return;
    }
    const { h, defineComponent, ref, reactive, computed, watch, onMounted, onUnmounted, nextTick } = Vue;

    // 本地渲染器注册表（layout 递归派发用）
    const REGISTRY = {};
    function register(type, comp) {
        REGISTRY[type] = comp;
        if (typeof window.__registerVizRenderer__ === 'function') {
            window.__registerVizRenderer__(type, comp);
        }
    }

    // —— 全局样式注入（仅一次）——
    const STYLE_ID = 'viz-interaction-style';
    if (!document.getElementById(STYLE_ID)) {
        const css = `
.viz-root { font-size: 14px; color: rgb(var(--text-rgb, 230,235,245)); }
.viz-title { font-weight: 600; margin-bottom: 8px; opacity: .9; }
.viz-fallback { padding: 8px 12px; background: rgba(255,180,80,.12); border: 1px dashed rgba(255,180,80,.5); border-radius: 6px; font-size: 12px; }
.viz-fallback pre { margin: 4px 0 0; white-space: pre-wrap; word-break: break-all; font-size: 11px; opacity: .8; }

/* choice */
.viz-choice { padding: 4px 0; }
.viz-choice .viz-q { font-weight: 600; margin-bottom: 10px; line-height: 1.5; }
.viz-choice .viz-opts { display: flex; flex-direction: column; gap: 8px; }
.viz-choice .viz-opt { text-align: left; padding: 10px 14px; border-radius: 8px; border: 1px solid rgba(var(--accent-rgb,120,140,255),.35); background: rgba(var(--accent-rgb,120,140,255),.08); color: inherit; cursor: pointer; transition: all .15s; font-size: 13px; }
.viz-choice .viz-opt:hover { background: rgba(var(--accent-rgb,120,140,255),.2); border-color: rgba(var(--accent-rgb,120,140,255),.6); }
.viz-choice .viz-opt.selected { background: rgba(var(--accent-rgb,120,140,255),.3); border-color: var(--accent-light,#9db4ff); }
.viz-choice .viz-opt:disabled { opacity: .55; cursor: default; }

/* fill */
.viz-fill { padding: 4px 0; }
.viz-fill .viz-q { font-weight: 600; margin-bottom: 10px; line-height: 1.5; }
.viz-fill .viz-blanks { display: flex; flex-direction: column; gap: 8px; }
.viz-fill .viz-blank { display: flex; align-items: center; gap: 8px; }
.viz-fill .viz-blank-label { flex-shrink: 0; min-width: 56px; font-size: 13px; opacity: .85; }
.viz-fill .viz-input { flex: 1; min-width: 0; padding: 8px 12px; border-radius: 8px; border: 1px solid rgba(255,255,255,.2); background: rgba(255,255,255,.06); color: inherit; font-size: 13px; outline: none; transition: all .15s; }
.viz-fill .viz-input:focus { border-color: rgba(var(--accent-rgb,120,140,255),.6); background: rgba(255,255,255,.09); }
.viz-fill .viz-input:disabled { opacity: .55; }

/* 提交按钮（choice / fill 通用）*/
.viz-submit { margin-top: 10px; padding: 8px 18px; border-radius: 8px; border: none; background: var(--accent-light,#9db4ff); color: #0a0e1a; font-weight: 600; cursor: pointer; transition: all .15s; }
.viz-submit:hover:not(:disabled) { filter: brightness(1.08); }
.viz-submit:disabled { opacity: .5; cursor: not-allowed; }

/* 统一提交栏（多表单模式）*/
.viz-multi-form { margin-bottom: 0; }
.viz-unified-bar {
    display: flex; align-items: center; justify-content: space-between;
    margin: 12px 0 4px; padding: 12px 16px;
    background: linear-gradient(135deg, rgba(var(--accent-rgb,120,140,255),.18), rgba(var(--accent-rgb,120,140,255),.06));
    border: 1px solid rgba(var(--accent-rgb,120,140,255),.4);
    border-radius: 10px;
    animation: viz-bar-in .25s ease;
}
@keyframes viz-bar-in { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
.viz-unified-title { font-size: 13px; color: rgba(255,255,255,.85); }
.viz-unified-title b { color: var(--accent-light,#9db4ff); margin: 0 2px; }
.viz-unified-submit { margin: 0; padding: 8px 20px; }

/* image */
.viz-image { display: flex; flex-direction: column; gap: 6px; max-width: 360px; }
.viz-image img { max-width: 100%; max-height: 240px; object-fit: contain; border-radius: 8px; cursor: zoom-in; border: 1px solid rgba(255,255,255,.08); background: rgba(255,255,255,.02); }
.viz-image .viz-cap { font-size: 12px; opacity: .65; text-align: center; }

/* flowchart */
.viz-flow { overflow-x: auto; overflow-y: hidden; padding: 4px; border: 1px solid rgba(255,255,255,.08); border-radius: 8px; }
.viz-flow svg { display: block; }
.viz-flow .viz-err { color: #ff8080; font-size: 12px; }
.viz-flow-bar { display: flex; justify-content: space-between; align-items: center; padding: 6px 10px; background: rgba(0,0,0,.25); border-bottom: 1px solid rgba(255,255,255,.06); font-size: 12px; }
.viz-flow-title { opacity: .8; }
.viz-flow-actions { display: flex; gap: 6px; }
.viz-flow-btn { padding: 4px 10px; border-radius: 5px; border: 1px solid rgba(255,255,255,.15); background: rgba(255,255,255,.06); color: inherit; font-size: 11px; cursor: pointer; transition: all .15s; }
.viz-flow-btn:hover { background: rgba(255,255,255,.12); border-color: rgba(255,255,255,.3); }
.viz-flow-btn-primary { background: var(--accent-light,#9db4ff); color: #0a0e1a; border-color: transparent; font-weight: 600; }
.viz-flow-btn-primary:hover { filter: brightness(1.08); }
.viz-flow-btn-close { background: transparent; border-color: rgba(255,255,255,.3); color: #fff; }
    /* 画布高度由组件内联动态设定（窗口自适应：内容显示高 + 边距，钳 [120, 窗口×0.55]），
       CSS 不再钳 max/min-height（会覆盖内联高度把底部裁掉） */
    .viz-flow-canvas { padding: 0; overflow: hidden; position: relative; }
/* 画布内 SVG 必须保持在文档流内（禁止 position:absolute）：
   若脱流，零宽 shrink-to-fit 的 .viz-viewport 会把 svg 的百分比宽度钳成 0×0，
   缩略图整片空白（全屏画布类名不同 .viz-flow-full-canvas 故不受影响）。 */
.viz-pan-layer { position: absolute; inset: 0; }
.viz-viewport { position: absolute; top: 0; left: 0; }
.viz-flow-full { position: fixed; inset: 0; z-index: 9999; background: rgba(0,0,0,.92); display: flex; flex-direction: column; }
.viz-flow-full-bar { display: flex; justify-content: space-between; align-items: center; padding: 8px 16px; color: #fff; font-size: 13px; border-bottom: 1px solid rgba(255,255,255,.08); }
.viz-flow-full-canvas { flex: 1; overflow: hidden; position: relative; }
.viz-flow-full-zoom { display: flex; align-items: center; gap: 6px; }
.viz-flow-zoom-pct { min-width: 46px; text-align: center; font-variant-numeric: tabular-nums; color: rgba(255,255,255,.85); font-size: 12px; }
.viz-flow-btn-reset { color: var(--accent-light,#9db4ff); border-color: rgba(var(--accent-rgb,120,140,255),.5); }

/* mindmap */
.viz-mind { overflow: hidden; padding: 0; border: 1px solid rgba(255,255,255,.08); border-radius: 8px; }
.viz-mind svg { display: block; }
.viz-mind .mm-node rect { fill: rgba(var(--accent-rgb,120,140,255),.15); stroke: rgba(var(--accent-rgb,120,140,255),.5); }
.viz-mind .mm-node text { fill: rgb(var(--text-rgb,230,235,245)); font-size: 12px; }

/* html */
.viz-html { position: relative; border: 1px solid rgba(255,255,255,.1); border-radius: 8px; overflow: hidden; }
.viz-html iframe { width: 100%; border: none; background: #0a0e1a; }
.viz-html .viz-html-bar { display: flex; justify-content: space-between; align-items: center; padding: 4px 8px; background: rgba(0,0,0,.3); font-size: 11px; }
.viz-html .viz-html-zoom { cursor: pointer; padding: 2px 8px; border-radius: 4px; border: 1px solid rgba(255,255,255,.2); background: transparent; color: inherit; }
.viz-html-full { position: fixed; inset: 0; z-index: 9999; background: rgba(0,0,0,.85); display: flex; flex-direction: column; }
.viz-html-full iframe { flex: 1; width: 100%; border: none; background: #0a0e1a; }
.viz-html-full .viz-html-full-bar { display: flex; justify-content: space-between; align-items: center; padding: 8px 16px; color: #fff; }
.viz-html-full .viz-html-close { cursor: pointer; padding: 4px 12px; border-radius: 4px; border: 1px solid rgba(255,255,255,.3); background: transparent; color: #fff; }
/* viz-html 容器自身滚动条（极小）*/
.viz-html { scrollbar-width: thin; scrollbar-color: rgba(157,180,255,.4) transparent; }
.viz-html::-webkit-scrollbar { width: 4px; height: 4px; }
.viz-html::-webkit-scrollbar-track { background: transparent; }
.viz-html::-webkit-scrollbar-thumb { background: rgba(157,180,255,.4); border-radius: 3px; }
.viz-html::-webkit-scrollbar-thumb:hover { background: rgba(157,180,255,.7); }

/* layout */
.viz-layout { display: grid; gap: 12px; }
.viz-layout .viz-cell { min-width: 0; }
.viz-layout .viz-cell-title { font-size: 12px; font-weight: 600; opacity: .7; margin-bottom: 4px; }

/* —— 通用 range slider 美化（小巧精致）—— */
input[type=range].viz-slider,
.viz-root input[type=range],
input[type=range] {
    -webkit-appearance: none; appearance: none;
    background: transparent; cursor: pointer;
    outline: none; padding: 0; margin: 0;
}
/* 水平轨道 */
input[type=range]::-webkit-slider-runnable-track {
    height: 3px; border-radius: 2px;
    background: rgba(var(--surface-rgb,120,140,255),0.15);
}
input[type=range]::-moz-range-track {
    height: 3px; border-radius: 2px;
    background: rgba(120,140,255,0.15); border: none;
}
/* 水平 thumb —— 小巧 10x10 */
input[type=range]::-webkit-slider-thumb {
    -webkit-appearance: none; appearance: none;
    width: 10px; height: 10px; border-radius: 50%;
    background: var(--accent-light,#9db4ff);
    box-shadow: 0 1px 3px rgba(0,0,0,.35), 0 0 0 1px rgba(255,255,255,.15) inset;
    margin-top: -3.5px;
    transition: transform .1s, box-shadow .1s;
}
input[type=range]::-webkit-slider-thumb:hover {
    transform: scale(1.2);
    box-shadow: 0 1px 4px rgba(0,0,0,.45), 0 0 0 1px rgba(255,255,255,.25) inset;
}
input[type=range]::-webkit-slider-thumb:active { transform: scale(1.05); }
input[type=range]::-moz-range-thumb {
    width: 10px; height: 10px; border-radius: 50%;
    background: #9db4ff;
    box-shadow: 0 1px 3px rgba(0,0,0,.35);
    border: none;
}
/* 垂直 slider：用 rotate 实现（跨内核兼容）*/
input[type=range].viz-vertical {
    writing-mode: vertical-lr;
    direction: rtl;
    width: auto; height: 100px;
}
input[type=range].viz-vertical::-webkit-slider-runnable-track {
    width: 3px; height: 100%;
}
input[type=range].viz-vertical::-webkit-slider-thumb {
    width: 10px; height: 10px;
    margin-top: 0;
    margin-left: -3.5px;
}
/* slider 容器（常见：带数值标签的一行）*/
.viz-slider-row { display: flex; align-items: center; gap: 8px; }
.viz-slider-row input[type=range] { flex: 1; min-width: 40px; }
.viz-slider-val { font-size: 11px; font-weight: 600; color: var(--accent-light,#9db4ff); font-variant-numeric: tabular-nums; min-width: 32px; text-align: right; }
`;
        const style = document.createElement('style');
        style.id = STYLE_ID;
        style.textContent = css;
        document.head.appendChild(style);
    }

    // —— 工具 ——
    function fallback(msg, raw) {
        return h('div', { class: 'viz-fallback' }, [
            h('div', null, '⚠️ ' + msg),
            raw ? h('pre', null, raw) : null
        ]);
    }
    function safeStr(v, d) { return (typeof v === 'string') ? v : (v == null ? d : String(v)); }

    // —— 缩略图窗口适配 fit（flowchart / mindmap 共用）——
    // 画布高度跟随「内容实际显示高度」（svgH×fitZoom），上限按窗口可视高度比例限制，
    // 下限 120px——避免旧实现 clamp(svgH,180,400) 与容器宽度脱钩导致的四周大片空白。
    // 内容超限（ZOOM_MIN 钳制后仍溢出）时 pan 左上对齐（居中会把两侧都推出可视区），拖拽/全屏可达。
    const VIZ_FIT_PAD = 16;          // 画布内边距（同旧 padH/padV）
    const VIZ_THUMB_MIN_H = 120;     // 缩略画布最小高度
    const ZOOM_MIN = 0.3, ZOOM_MAX = 3.0;
    function vizThumbMaxH() {
        // 与 html 缩略图同款上限公式：窗口高度 × 0.55，钳 [160, 520]
        const vh = (typeof window !== 'undefined' && window.innerHeight) || 800;
        return Math.max(160, Math.min(520, Math.round(vh * 0.55)));
    }
    function vizPan(avail, c, pad) {
        // 内容 c 不超可用宽 avail 时居中；超出时左/上对齐（pad 起点）
        return c <= avail ? (avail - c) / 2 + pad : pad;
    }
    // 缩略图 fit：availW = 容器可用宽（画布高度由本函数产出，不需入参高度）
    function vizThumbFit(svgW, svgH, availW) {
        const pad = VIZ_FIT_PAD, maxH = vizThumbMaxH();
        const availHMax = maxH - pad * 2; // 画布最高时对应的可用高
        const z = +Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, Math.min(availW / svgW, availHMax / svgH))).toFixed(3);
        const cw = svgW * z, ch = svgH * z;
        // 画布高 = 显示高 + 双边距，钳 [MIN, maxH]；MIN 钳高的小图垂直居中留少量空白
        const wantH = Math.min(Math.max(Math.ceil(ch) + pad * 2, VIZ_THUMB_MIN_H), maxH);
        const availH = wantH - pad * 2;
        return {
            z,
            panX: +vizPan(availW, cw, pad).toFixed(2),
            panY: +vizPan(availH, ch, pad).toFixed(2),
            wantH
        };
    }
    // 全屏 fit：可用宽高均来自全屏画布 rect
    function vizFullFit(svgW, svgH, availW, availH) {
        const pad = VIZ_FIT_PAD;
        const z = +Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, Math.min(availW / svgW, availH / svgH))).toFixed(3);
        const cw = svgW * z, ch = svgH * z;
        return {
            z,
            panX: +vizPan(availW, cw, pad).toFixed(2),
            panY: +vizPan(availH, ch, pad).toFixed(2)
        };
    }
    // 暴露纯函数（layout 嵌套复用 / 测试钩子）
    window.__VIZ_FIT__ = { vizThumbMaxH, vizThumbFit, vizFullFit, vizPan, ZOOM_MIN, ZOOM_MAX, VIZ_FIT_PAD, VIZ_THUMB_MIN_H };

    // —— 全局 form 注册表 + 统一提交协调器 ——
    // 收集所有 choice/fill 实例；当同页面出现 >=2 个未提交 form 时，隐藏各自提交按钮，
    // 在最后一个 form 下方插入统一提交栏。
    window.__vizFormRegistry__ = window.__vizFormRegistry__ || [];
    window.__vizFormUnifiedBar__ = null; // 统一提交栏 DOM 引用

    function _vizEnsureUnifiedBar(activeForms) {
        // 找最后一个活跃 form 的 DOM
        const last = activeForms[activeForms.length - 1];
        if (!last || !last.el) return;
        const scope = last.scope;
        // 已存在 bar 且就在正确位置（同 scope 同锚点）
        if (window.__vizFormUnifiedBar__ && window.__vizFormUnifiedBar__._afterEl === last.el
            && window.__vizFormUnifiedBar__._scope === scope) return;
        // 清理旧的
        if (window.__vizFormUnifiedBar__) {
            window.__vizFormUnifiedBar__.remove();
            window.__vizFormUnifiedBar__ = null;
        }
        // 创建新的统一提交栏（原生 DOM，不依赖 Vue）
        const bar = document.createElement('div');
        bar.className = 'viz-unified-bar';
        bar._afterEl = last.el;
        bar._scope = scope;
        bar.innerHTML = `
            <span class="viz-unified-title">共 <b>${activeForms.length}</b> 道题</span>
            <button class="viz-submit viz-unified-submit">提交</button>
        `;
        bar.querySelector('button').addEventListener('click', () => {
            const reg = window.__vizFormRegistry__ || [];
            // 只提交本消息容器内的 form，避免把历史消息的选项一起再次提交
            const pending = reg.filter(f => !f.submitted && !f.unmounted && f.scope === scope);
            if (!pending.length) return;
            const answers = pending.map((f, idx) => {
                const q = f.question ? `Q${idx + 1}: ${f.question}\n` : '';
                return q + f.getAnswer();
            });
            const api = pending[0].extAPI;
            if (api && api.sendUserInput) api.sendUserInput(answers.join('\n\n'));
            // 标记全部已提交（触发各组件的 submitted 状态）
            pending.forEach(f => { f.submitted = true; f.onSubmitted && f.onSubmitted(); });
            bar.querySelector('button').disabled = true;
            bar.querySelector('button').textContent = '已统一提交 ✓';
            setTimeout(() => { bar.remove(); window.__vizFormUnifiedBar__ = null; }, 1800);
        });
        // 插入到最后一个 form DOM 之后
        last.el.parentNode && last.el.parentNode.insertBefore(bar, last.el.nextSibling);
        window.__vizFormUnifiedBar__ = bar;
    }

    // 协调器：每 200ms 扫描 registry
    setInterval(() => {
        const reg = window.__vizFormRegistry__ || [];
        // 清理已卸载
        const alive = reg.filter(f => !f.unmounted);
        if (alive.length !== reg.length) {
            window.__vizFormRegistry__ = alive;
        }
        // scope（消息容器 .bubble）隔离：
        // 只有文档位置最靠后的 scope（即最新一条带 form 的消息）里的 form 可交互；
        // 历史消息里遗留的未提交 form 一律冻结，防止新一次提交把之前对话中的选项一起再次提交。
        const withEl = alive.filter(f => f.el);
        let latestScope = null, latestEl = null;
        withEl.forEach(f => {
            if (!latestEl || (latestEl.compareDocumentPosition(f.el) & Node.DOCUMENT_POSITION_FOLLOWING)) {
                latestEl = f.el; latestScope = f.scope;
            }
        });
        alive.forEach(f => {
            if (!f.frozen && !f.submitted && latestScope != null && f.scope !== latestScope) {
                f.frozen = true;
                f.onFrozen && f.onFrozen();
            }
        });
        // 标记多表单模式：只统计同一消息容器内未提交的 form
        const unsubmitted = alive.filter(f => !f.submitted && !f.frozen);
        const scopeCount = new Map();
        unsubmitted.forEach(f => scopeCount.set(f.scope, (scopeCount.get(f.scope) || 0) + 1));
        alive.forEach(f => {
            const isMulti = (scopeCount.get(f.scope) || 0) >= 2;
            if (f.multiRef) f.multiRef.value = isMulti;
            if (f.el) {
                if (isMulti) f.el.classList.add('viz-multi-form');
                else f.el.classList.remove('viz-multi-form');
            }
        });
        // 统一提交栏管理（只在最新消息的 scope 内创建）
        const latestGroup = unsubmitted.filter(f => f.scope === latestScope);
        if (latestGroup.length >= 2) {
            _vizEnsureUnifiedBar(latestGroup);
        } else {
            if (window.__vizFormUnifiedBar__) {
                window.__vizFormUnifiedBar__.remove();
                window.__vizFormUnifiedBar__ = null;
            }
        }
    }, 200);

    // ============ choice ============
    // 选择题：单选/多选；多表单模式下隐藏自己的提交按钮，由统一提交栏接管
    const Choice = defineComponent({
        props: { data: Object, raw: String, extAPI: Object },
        setup(props) {
            const rootEl = ref(null);
            const selected = ref(new Set());
            const submitted = ref(false);
            const frozen = ref(false);
            const multiRef = ref(false);
            const data = props.data || {};
            const multiple = !!data.multiple;
            const options = Array.isArray(data.options) ? data.options : [];
            function optLabel(o) { return typeof o === 'string' ? o : safeStr(o && o.label, ''); }
            function optValue(o) { return typeof o === 'string' ? o : (o && o.value != null ? o.value : optLabel(o)); }
            function getAnswer() {
                const vals = [...selected.value].map(i => optValue(options[i]));
                return vals.map(v => typeof v === 'object' ? JSON.stringify(v) : String(v)).join(', ') || '(未作答)';
            }
            function toggle(i) {
                if (submitted.value || frozen.value) return;
                if (multiple) {
                    const s = new Set(selected.value);
                    s.has(i) ? s.delete(i) : s.add(i);
                    selected.value = s;
                } else {
                    selected.value = new Set([i]);
                }
            }
            function submit() {
                if (submitted.value || frozen.value || selected.value.size === 0) return;
                // 多表单模式下 submit 不直接发，只标记 — 但实际上 multi 模式下按钮不显示
                // 这里保留以防 Enter key 等触发
                submitted.value = true;
                if (!multiRef.value && props.extAPI && props.extAPI.sendUserInput) {
                    props.extAPI.sendUserInput(getAnswer());
                }
            }
            onMounted(() => {
                window.__vizFormRegistry__.push({
                    el: rootEl.value,
                    type: 'choice',
                    question: safeStr(data.question, ''),
                    // scope = 所属消息容器（.bubble），用于把提交范围限制在本条消息内
                    scope: rootEl.value ? (rootEl.value.closest('.bubble') || rootEl.value) : null,
                    getAnswer,
                    submitted: false,
                    multiRef,
                    extAPI: props.extAPI,
                    unmounted: false,
                    onSubmitted: () => { submitted.value = true; },
                    onFrozen: () => { frozen.value = true; }
                });
            });
            onUnmounted(() => {
                const reg = window.__vizFormRegistry__ || [];
                const idx = reg.findIndex(f => f.getAnswer === getAnswer);
                if (idx >= 0) reg[idx].unmounted = true;
            });
            return () => {
                const showBtn = !multiRef.value;
                return h('div', { class: 'viz-choice viz-root', ref: rootEl }, [
                    h('div', { class: 'viz-q' }, safeStr(data.question, '')),
                    h('div', { class: 'viz-opts' },
                        options.map((o, i) => h('button', {
                            class: 'viz-opt' + (selected.value.has(i) ? ' selected' : ''),
                            disabled: submitted.value || frozen.value,
                            onClick: () => toggle(i)
                        }, optLabel(o))
                        )),
                    showBtn ? h('button', {
                        class: 'viz-submit',
                        disabled: submitted.value || frozen.value || selected.value.size === 0,
                        onClick: submit
                    }, submitted.value ? '已提交 ✓' : (frozen.value ? '已过期' : (multiple ? `提交选择（已选 ${selected.value.size} 项）` : '提交'))) : null
                ]);
            };
        }
    });

    // ============ fill ============
    // 填空题；多表单模式下隐藏自己的提交按钮
    const Fill = defineComponent({
        props: { data: Object, raw: String, extAPI: Object },
        setup(props) {
            const rootEl = ref(null);
            const data = props.data || {};
            const submitted = ref(false);
            const frozen = ref(false);
            const multiRef = ref(false);
            const blanks = (Array.isArray(data.blanks) ? data.blanks : []).map(b =>
                typeof b === 'string' ? { label: b } : (b || {})
            );
            if (!blanks.length && (data.placeholder != null || data.label != null)) {
                blanks.push({ label: data.label || '', placeholder: data.placeholder || '' });
            }
            const values = ref(blanks.map(() => ''));
            const canSubmit = computed(() => values.value.some(v => (v || '').trim()));
            function getAnswer() {
                const parts = blanks.map((b, i) => {
                    const v = (values.value[i] || '').trim();
                    if (!v) return '';
                    return b.label ? (b.label + ': ' + v) : v;
                }).filter(Boolean);
                return parts.join(' / ') || '(未作答)';
            }
            function submit() {
                if (submitted.value || frozen.value || !canSubmit.value) return;
                submitted.value = true;
                if (!multiRef.value && props.extAPI && props.extAPI.sendUserInput) {
                    props.extAPI.sendUserInput(getAnswer());
                }
            }
            onMounted(() => {
                window.__vizFormRegistry__.push({
                    el: rootEl.value,
                    type: 'fill',
                    question: safeStr(data.question, ''),
                    // scope = 所属消息容器（.bubble），用于把提交范围限制在本条消息内
                    scope: rootEl.value ? (rootEl.value.closest('.bubble') || rootEl.value) : null,
                    getAnswer,
                    submitted: false,
                    multiRef,
                    extAPI: props.extAPI,
                    unmounted: false,
                    onSubmitted: () => { submitted.value = true; },
                    onFrozen: () => { frozen.value = true; }
                });
            });
            onUnmounted(() => {
                const reg = window.__vizFormRegistry__ || [];
                const idx = reg.findIndex(f => f.getAnswer === getAnswer);
                if (idx >= 0) reg[idx].unmounted = true;
            });
            return () => {
                const showBtn = !multiRef.value;
                return h('div', { class: 'viz-fill viz-root', ref: rootEl }, [
                    h('div', { class: 'viz-q' }, safeStr(data.question, '')),
                    h('div', { class: 'viz-blanks' },
                        blanks.map((b, i) => h('div', { class: 'viz-blank' }, [
                            b.label ? h('label', { class: 'viz-blank-label' }, b.label) : null,
                            h('input', {
                                class: 'viz-input',
                                type: b.type === 'number' ? 'number' : 'text',
                                placeholder: safeStr(b.placeholder, ''),
                                value: values.value[i],
                                disabled: submitted.value || frozen.value,
                                onInput: (e) => { values.value[i] = e.target.value; },
                                // 多表单模式下 Enter 也不单独提交，等统一提交栏
                                onKeydown: (e) => { if (e.key === 'Enter' && !multiRef.value) submit(); }
                            })
                        ]))
                    ),
                    showBtn ? h('button', {
                        class: 'viz-submit',
                        disabled: submitted.value || frozen.value || !canSubmit.value,
                        onClick: submit
                    }, submitted.value ? '已提交 ✓' : (frozen.value ? '已过期' : '提交')) : null
                ]);
            };
        }
    });

    // ============ image ============
    const Image_ = defineComponent({
        props: { data: Object, raw: String, extAPI: Object },
        setup(props) {
            const data = props.data || {};
            const err = ref(false);
            function resolveUrl(u) {
                if (!u) return '';
                // 远程 http(s) / data: / blob: / file: 直接用
                if (/^(https?:|data:|blob:|file:|\/)/i.test(u)) return u;
                // 否则视为本地路径，走 extAPI.imageURL
                return (props.extAPI && props.extAPI.imageURL) ? props.extAPI.imageURL(u) : u;
            }
            function onZoom() {
                const url = resolveUrl(data.url);
                if (props.extAPI && props.extAPI.openLightbox) props.extAPI.openLightbox(url);
            }
            return () => {
                if (!data.url) return fallback('image 缺少 url 字段', props.raw);
                if (err.value) return fallback('image 加载失败: ' + safeStr(data.url, ''), props.raw);
                return h('div', { class: 'viz-image viz-root' }, [
                    h('img', {
                        src: resolveUrl(data.url),
                        alt: safeStr(data.caption, ''),
                        style: data.width ? { width: data.width + 'px' } : null,
                        onClick: onZoom,
                        onError: () => { err.value = true; }
                    }),
                    data.caption ? h('div', { class: 'viz-cap' }, safeStr(data.caption, '')) : null
                ]);
            };
        }
    });

    // ============ flowchart ============
    // 自适布局 + 重心排序 + 节点拖拽 + 全屏弹窗
    const Flowchart = defineComponent({
        props: { data: Object, raw: String, extAPI: Object },
        setup(props) {
            // —— 工具函数 ——
            function estimateTextPx(text) {
                let w = 0;
                for (const ch of text) w += (/[\u4e00-\u9fa5]/.test(ch) ? 12 : 7);
                return w;
            }
            function nodeSize(label) {
                const PAD_X = 24, PAD_Y = 14, FONT = 12, lineH = FONT + 4;
                const MAX_W = 200, MIN_W = 80;
                const rawW = estimateTextPx(label);
                let targetW = Math.min(Math.max(rawW + PAD_X * 2, MIN_W), MAX_W);
                let lines = [];
                if (rawW + PAD_X * 2 <= MAX_W) {
                    lines = [label];
                } else {
                    const mid = Math.floor(label.length / 2);
                    lines = [label.slice(0, mid), label.slice(mid)];
                }
                const h = PAD_Y * 2 + lineH * lines.length;
                return { width: targetW, height: Math.max(h, 36), lines };
            }
            function topoLayers(ids, edges) {
                const indeg = {}; ids.forEach(id => indeg[id] = 0);
                const adj = {}; ids.forEach(id => adj[id] = []);
                edges.forEach(e => { if (indeg[e.to] != null) indeg[e.to]++; if (adj[e.from] != null) adj[e.from].push(e.to); });
                const layers = []; const placed = new Set(); let remaining = ids.slice();
                while (remaining.length) {
                    const layer = remaining.filter(id => indeg[id] === 0);
                    if (!layer.length) { layers.push(remaining.slice()); break; }
                    layers.push(layer);
                    layer.forEach(id => { placed.add(id); adj[id].forEach(t => indeg[t]--); });
                    remaining = remaining.filter(id => !placed.has(id));
                }
                return layers;
            }
            function barycenterSort(layers, edges) {
                if (layers.length <= 1) return layers;
                const parents = {};
                layers.forEach(layer => layer.forEach(id => parents[id] = []));
                edges.forEach(e => { if (parents[e.to]) parents[e.to].push(e.from); });
                const MAX_PASSES = 4;
                let cur = layers.map(l => l.slice());
                for (let pass = 0; pass < MAX_PASSES; pass++) {
                    for (let li = 1; li < cur.length; li++) {
                        const prev = cur[li - 1];
                        cur[li] = cur[li].slice().sort((a, b) => {
                            const avg = (arr) => arr.reduce((s, id) => s + prev.indexOf(id), 0) / (arr.length || 1);
                            const sa = avg(parents[a] || []), sb = avg(parents[b] || []);
                            if (sa !== sb) return sa - sb; return a.localeCompare(b);
                        });
                    }
                    if (pass < MAX_PASSES - 1) {
                        const children = {};
                        layers.forEach(layer => layer.forEach(id => children[id] = []));
                        edges.forEach(e => { if (children[e.from]) children[e.from].push(e.to); });
                        for (let li = cur.length - 2; li >= 0; li--) {
                            const next = cur[li + 1];
                            cur[li] = cur[li].slice().sort((a, b) => {
                                const avg = (arr) => arr.reduce((s, id) => s + next.indexOf(id), 0) / (arr.length || 1);
                                const sa = avg(children[a] || []), sb = avg(children[b] || []);
                                if (sa !== sb) return sa - sb; return a.localeCompare(b);
                            });
                        }
                    }
                }
                return cur;
            }
            // 计算初始布局（{ pos, sizeMap, svgW, svgH, edges, rawNodes }）
            function computeLayout(data) {
                const rawNodes = Array.isArray(data.nodes) ? data.nodes : [];
                const edges = Array.isArray(data.edges) ? data.edges : [];
                const sizeMap = {};
                rawNodes.forEach(n => {
                    const label = safeStr(n && n.label, safeStr(n && n.id, ''));
                    sizeMap[n.id] = nodeSize(label);
                });
                const ids = rawNodes.map(n => n.id);
                const layers = barycenterSort(topoLayers(ids, edges), edges);
                const GX = 48, GY = 40, PAD_SIDE = 20;
                const layerInfo = layers.map(layerIds => {
                    let totalW = PAD_SIDE * 2, maxH = 0;
                    layerIds.forEach((id, i) => {
                        const s = sizeMap[id];
                        totalW += s.width + (i > 0 ? GX : 0);
                        if (s.height > maxH) maxH = s.height;
                    });
                    return { totalW, maxH };
                });
                const svgH = PAD_SIDE + layerInfo.reduce((s, li) => s + li.maxH, 0) + GY * (layers.length - 1) + PAD_SIDE;
                const svgW = Math.max(...layerInfo.map(li => li.totalW), 200);
                const pos = {}; let curY = PAD_SIDE;
                layers.forEach((layerIds, li) => {
                    const info = layerInfo[li];
                    let curX = (svgW - info.totalW) / 2 + PAD_SIDE;
                    layerIds.forEach(id => {
                        const s = sizeMap[id];
                        pos[id] = { x: curX, y: curY, w: s.width, h: s.height, lines: s.lines };
                        curX += s.width + GX;
                    });
                    curY += info.maxH + GY;
                });
                return { pos, sizeMap, svgW, svgH, edges, rawNodes };
            }
            // 生成完整 SVG 渲染树（render 函数和全屏弹窗复用）
            function buildSvgNodes(pos, layout) {
                const accentColor = 'rgba(var(--accent-rgb,120,140,255),';
                const textColor = 'rgb(var(--text-rgb,230,235,245))';
                const svg = h('svg', {
                    width: layout.svgW, height: layout.svgH,
                    viewBox: `0 0 ${layout.svgW} ${layout.svgH}`,
                    xmlns: 'http://www.w3.org/2000/svg'
                }, [
                    // 连线
                    ...layout.edges.map((e, ei) => {
                        const a = pos[e.from], b = pos[e.to];
                        if (!a || !b) return null;
                        const x1 = a.x + a.w / 2, y1 = a.y + a.h;
                        const x2 = b.x + b.w / 2, y2 = b.y;
                        const midY = (y1 + y2) / 2;
                        const pathD = `M${x1},${y1} C${x1},${midY} ${x2},${midY} ${x2},${y2}`;
                        return h('g', { key: 'e' + ei }, [
                            h('path', { d: pathD, fill: 'none', stroke: accentColor + '.55)', 'stroke-width': 1.5 }),
                            h('polygon', { points: `${x2 - 4},${y2 - 7} ${x2 + 4},${y2 - 7} ${x2},${y2}`, fill: accentColor + '.8)' }),
                            e.label ? h('text', { x: (x1 + x2) / 2, y: midY - 4, 'text-anchor': 'middle', 'font-size': 10, fill: 'rgba(255,255,255,.65)' }, safeStr(e.label, '')) : null
                        ]);
                    }),
                    // 节点（带拖拽事件）
                    ...layout.rawNodes.map((n, ni) => {
                        const p = pos[n.id]; if (!p) return null;
                        return h('g', {
                            key: 'n' + ni,
                            transform: `translate(${p.x},${p.y})`,
                            style: { cursor: 'grab' },
                            onMousedown: (ev) => {
                                ev.preventDefault();
                                draggingId.value = n.id;
                                // 先记录 canvas rect，用它把 clientX 转 canvas-relative
                                const canvasEl = getCanvasEl();
                                const canvasRect = canvasEl ? canvasEl.getBoundingClientRect() : { left: 0, top: 0 };
                                dragOffset._rect = canvasRect;
                                const cx = ev.clientX - canvasRect.left;
                                const cy = ev.clientY - canvasRect.top;
                                // dragOffset 存画布坐标差: 画布 = (canvas-local - pan) / zoom
                                dragOffset.dx = (cx - panX.value) / zoom.value - p.x;
                                dragOffset.dy = (cy - panY.value) / zoom.value - p.y;
                            }
                        }, [
                            h('rect', {
                                width: p.w, height: p.h, rx: 8,
                                fill: accentColor + '.15)',
                                stroke: accentColor + '.55)',
                                'stroke-width': 1.2
                            }),
                            ...p.lines.map((line, li) => {
                                const totalLineH = p.lines.length * 16;
                                const startY = (p.h - totalLineH) / 2 + 12;
                                return h('text', {
                                    x: p.w / 2, y: startY + li * 16,
                                    'text-anchor': 'middle', 'font-size': 12, fill: textColor,
                                    'pointer-events': 'none'
                                }, line);
                            })
                        ]);
                    })
                ]);
                return svg;
            }

            // —— 状态 ——
            const full = ref(false);
            const zoom = ref(1);          // 画布缩放（主画布/全屏共用）
            const panX = ref(0);          // 画布平移 X
            const panY = ref(0);          // 画布平移 Y
            const fitMode = ref(true);    // 缩略图模式：自动 fit 到容器
            const draggingId = ref(null);  // 节点拖拽
            const dragOffset = reactive({ dx: 0, dy: 0 });
            const panning = ref(false);   // 画布平移模式
            const panStart = reactive({ mx: 0, my: 0, px: 0, py: 0 });
            const layout = computeLayout(props.data || {});
            // 缩略图画布高度（响应式）：跟随内容实际显示高（svgH×fitZoom）+ 边距，
            // 上限按窗口可视高度自适应（vizThumbFit 产出并更新），容器/窗口变化自动重算。
            // 旧实现 clamp(svgH,180,400) 与容器宽度脱钩：宽扁图 fit 后显示高很小，
            // 画布仍撑 180~400 → 四周大片空白。
            const canvasH = ref(Math.min(220, vizThumbMaxH()));
            const pos = reactive({});
            Object.keys(layout.pos).forEach(k => { pos[k] = { ...layout.pos[k] }; });

            // —— 缩略图 fit 计算 ——
            // 用 Vue ref 精确定位自己的 canvas DOM（多实例不串扰）
            const _canvasRef = ref(null);       // 主画布（viz-flow-canvas）
            const _fullCanvasRef = ref(null);   // 全屏画布（打开后才有）
            let _ro = null;                     // ResizeObserver
            let _fitTimer = null;               // fit 兜底重试定时器
            function getMyCanvas() {
                if (full.value) return _fullCanvasRef.value;
                return _canvasRef.value;
            }
            function fitToContainer() {
                const canvas = getMyCanvas();
                if (!canvas) return false;
                const rect = canvas.getBoundingClientRect();
                if (rect.width < 1 || rect.height < 1) return false;
                const s = layout.svgW, t = layout.svgH;
                if (s <= 0 || t <= 0) return false;
                if (full.value) {
                    // 全屏：可用宽高均来自全屏画布 rect
                    const f = vizFullFit(s, t, rect.width - VIZ_FIT_PAD * 2, rect.height - VIZ_FIT_PAD * 2);
                    zoom.value = f.z; panX.value = f.panX; panY.value = f.panY;
                    return true;
                }
                // 缩略：画布高度由 vizThumbFit 产出（内容显示高 + 边距，钳 [MIN, 窗口×0.55]）
                const tf = vizThumbFit(s, t, rect.width - VIZ_FIT_PAD * 2);
                zoom.value = tf.z; panX.value = tf.panX; panY.value = tf.panY;
                canvasH.value = tf.wantH; // 同值赋值不触发重渲染，收敛无震荡
                return true;
            }
            function getCanvasEl() { return getMyCanvas(); }
            function clientToCanvas(ev) {
                const el = getMyCanvas();
                if (!el) return { x: ev.clientX, y: ev.clientY };
                const r = el.getBoundingClientRect();
                return { x: ev.clientX - r.left, y: ev.clientY - r.top };
            }
            function _scheduleFit() {
                // 多帧重试，确保 DOM 尺寸就绪；容器尚未完成布局（rect 无效）时定时兜底重试，
                // 避免 fit 静默失败后缩略图永远停在空白/错位状态
                if (_fitTimer) { clearInterval(_fitTimer); _fitTimer = null; }
                requestAnimationFrame(() => {
                    fitToContainer();
                    requestAnimationFrame(() => fitToContainer());
                });
                let tries = 0;
                _fitTimer = setInterval(() => {
                    if (fitToContainer() || ++tries >= 10) { clearInterval(_fitTimer); _fitTimer = null; }
                }, 120);
            }
            // 窗口尺寸变化：RO 只观察画布自身（宽度变才触发），窗口高度变化需在此重算上限。
            // 手动缩放模式（fitMode=false）仅钳画布高度到新上限，不打断用户 pan/zoom 状态
            function onWinResize() {
                if (fitMode.value) { fitToContainer(); return; }
                if (!full.value) canvasH.value = Math.min(canvasH.value, vizThumbMaxH());
            }
            onMounted(() => {
                _scheduleFit();
                // ResizeObserver：容器尺寸变化自动重新 fit
                _ro = new ResizeObserver(() => { if (fitMode.value) fitToContainer(); });
                // 等 ref 就绪后 observe
                requestAnimationFrame(() => {
                    if (_canvasRef.value) _ro.observe(_canvasRef.value);
                });
                window.addEventListener('mousemove', onMouseMove);
                window.addEventListener('mouseup', onMouseUp);
                window.addEventListener('resize', onWinResize);
            });
            // full 切换：重新 observe 新的 canvas + fit
            watch(full, (v) => {
                if (_ro) _ro.disconnect();
                if (v) {
                    // 全屏 canvas 在 DOM 中了（条件渲染），等一帧 ref 就绪
                    requestAnimationFrame(() => {
                        if (_fullCanvasRef.value) { _ro.observe(_fullCanvasRef.value); _scheduleFit(); }
                    });
                } else {
                    requestAnimationFrame(() => {
                        if (_canvasRef.value) { _ro.observe(_canvasRef.value); _scheduleFit(); }
                    });
                }
            });
            // CSS transform 从右向左应用: translate(panX,panY) scale(zoom) = scale 先，translate 后
            // 画布→屏幕: sx = cx*zoom + panX    屏幕→画布: cx = (sx - panX)/zoom
            // （ZOOM_MIN/ZOOM_MAX 已提升为模块级常量，与 mindmap 共用）
            function doZoomAt(mx, my, delta) {
                fitMode.value = false;
                const oldZ = zoom.value;
                const newZ = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, +(oldZ + delta).toFixed(2)));
                if (newZ === oldZ) return;
                const ratio = newZ / oldZ;
                panX.value = +(panX.value * ratio + mx * (1 - ratio)).toFixed(2);
                panY.value = +(panY.value * ratio + my * (1 - ratio)).toFixed(2);
                zoom.value = newZ;
            }
            function resetPanZoom() { zoom.value = 1; panX.value = 0; panY.value = 0; fitMode.value = true; }
            function onCanvasWheel(ev) {
                ev.preventDefault();
                fitMode.value = false;
                const step = ev.ctrlKey || ev.metaKey ? 0.1 : 0.06;
                const p = clientToCanvas(ev);
                doZoomAt(p.x, p.y, ev.deltaY < 0 ? step : -step);
            }
            function onCanvasMouseDown(ev) {
                if (ev.button !== 0) return;
                fitMode.value = false;
                const target = ev.target;
                const isNode = target.closest && target.closest('g');
                // 记录 canvas rect 供后续全局 mousemove 使用
                const canvasEl = getCanvasEl();
                const canvasRect = canvasEl ? canvasEl.getBoundingClientRect() : { left: 0, top: 0 };
                dragOffset._rect = canvasRect;
                if (!isNode) {
                    panning.value = true;
                    panStart.mx = ev.clientX - canvasRect.left;
                    panStart.my = ev.clientY - canvasRect.top;
                    panStart.px = panX.value;
                    panStart.py = panY.value;
                    ev.preventDefault();
                }
            }

            // —— 全局事件监听（节点拖拽 + 画布平移）——
            function onMouseMove(ev) {
                const rect = dragOffset._rect || { left: 0, top: 0 };
                const cx = ev.clientX - rect.left;
                const cy = ev.clientY - rect.top;
                if (draggingId.value) {
                    // 画布坐标 = (屏幕坐标 - pan) / zoom
                    pos[draggingId.value].x = (cx - panX.value) / zoom.value - dragOffset.dx;
                    pos[draggingId.value].y = (cy - panY.value) / zoom.value - dragOffset.dy;
                } else if (panning.value) {
                    panX.value = panStart.px + (cx - panStart.mx);
                    panY.value = panStart.py + (cy - panStart.my);
                }
            }
            function onMouseUp() {
                draggingId.value = null;
                panning.value = false;
            }
            onUnmounted(() => {
                window.removeEventListener('mousemove', onMouseMove);
                window.removeEventListener('mouseup', onMouseUp);
                window.removeEventListener('resize', onWinResize);
                if (_ro) { _ro.disconnect(); _ro = null; }
                if (_fitTimer) { clearInterval(_fitTimer); _fitTimer = null; }
            });

            // —— 重置位置 ——
            function resetPositions() {
                const fresh = computeLayout(props.data || {});
                Object.keys(fresh.pos).forEach(k => {
                    if (pos[k]) {
                        pos[k].x = fresh.pos[k].x;
                        pos[k].y = fresh.pos[k].y;
                        pos[k].w = fresh.pos[k].w;
                        pos[k].h = fresh.pos[k].h;
                        pos[k].lines = fresh.pos[k].lines;
                    } else {
                        pos[k] = { ...fresh.pos[k] };
                    }
                });
            }

            // —— 工具栏按钮缩放（以画布中心为定点）——
            function toolbarZoom(delta) {
                // doZoomAt 锚点是画布局部坐标（非 client 坐标），以画布中心为定点
                const canvas = getCanvasEl();
                if (!canvas) return doZoomAt(0, 0, delta);
                const rect = canvas.getBoundingClientRect();
                doZoomAt(rect.width / 2, rect.height / 2, delta);
            }

            // —— 渲染 ——
            return () => {
                if (!layout.rawNodes.length) return fallback('flowchart 缺少 nodes', props.raw);
                const cursor = draggingId.value ? 'grabbing' : (panning.value ? 'grabbing' : 'grab');
                const panStyle = { transform: `translate(${panX.value}px, ${panY.value}px)` };
                const vpStyle = { zoom: zoom.value };
                return h('div', { class: 'viz-flow viz-root' }, [
                    // 工具栏
                    h('div', { class: 'viz-flow-bar' }, [
                        h('div', { class: 'viz-flow-actions' }, [
                            h('button', { class: 'viz-flow-btn', onClick: () => { resetPositions(); fitMode.value = true; fitToContainer(); } }, '↺ 重置'),
                            h('button', { class: 'viz-flow-btn', onClick: () => toolbarZoom(-0.1) }, '−'),
                            h('span', { class: 'viz-flow-zoom-pct' }, Math.round(zoom.value * 100) + '%'),
                            h('button', { class: 'viz-flow-btn', onClick: () => toolbarZoom(0.1) }, '+'),
                            h('button', { class: 'viz-flow-btn viz-flow-btn-primary', onClick: () => { full.value = true; fitMode.value = true; } }, '⛶')
                        ])
                    ]),
                    // SVG 画布（两层分离：pan-layer 纯平移 + viewport CSS zoom，同集群画布）
                    h('div', {
                        ref: _canvasRef,
                        class: 'viz-flow-canvas',
                        style: { cursor, height: canvasH.value + 'px' },
                        onWheel: onCanvasWheel,
                        onMousedown: onCanvasMouseDown
                    }, [
                        h('div', { class: 'viz-pan-layer', style: panStyle }, [
                            h('div', { class: 'viz-viewport', style: vpStyle }, [buildSvgNodes(pos, layout)])
                        ])
                    ]),
                    // 全屏弹窗
                    full.value ? h('div', {
                        class: 'viz-flow-full',
                        onWheel: onCanvasWheel,
                        onClick: (e) => { if (e.target.classList.contains('viz-flow-full')) full.value = false; }
                    }, [
                        h('div', { class: 'viz-flow-full-bar' }, [
                            h('div', { style: 'flex:1' }),
                            h('div', { class: 'viz-flow-full-zoom' }, [
                                h('button', { class: 'viz-flow-btn', onClick: () => toolbarZoom(-0.1) }, '−'),
                                h('span', { class: 'viz-flow-zoom-pct' }, Math.round(zoom.value * 100) + '%'),
                                h('button', { class: 'viz-flow-btn', onClick: () => toolbarZoom(0.1) }, '+'),
                                h('button', { class: 'viz-flow-btn viz-flow-btn-reset', onClick: resetPanZoom }, '1:1'),
                                h('button', { class: 'viz-flow-btn viz-flow-btn-close', onClick: () => { full.value = false; } }, '关闭 ✕')
                            ])
                        ]),
                        h('div', {
                            ref: _fullCanvasRef,
                            class: 'viz-flow-full-canvas',
                            style: { cursor },
                            onMousedown: onCanvasMouseDown
                        }, [
                            h('div', { class: 'viz-pan-layer', style: panStyle }, [
                                h('div', { class: 'viz-viewport', style: vpStyle }, [buildSvgNodes(pos, layout)])
                            ])
                        ])
                    ]) : null
                ]);
            };
        }
    });

    // ============ mindmap ============
    // 横向树布局 + 自适应节点尺寸 + 节点拖拽 + 全屏缩放（与 flowchart 一致）
    const Mindmap = defineComponent({
        props: { data: Object, raw: String, extAPI: Object },
        setup(props) {
            // —— 节点尺寸（自适应 + 折行，同 flowchart）——
            const NH = 8, DX = 60;          // 节点间垂直/水平间距
            const MAX_W = 180, MIN_W = 80;
            function estimateTextPx(text) {
                let w = 0;
                for (const ch of text) w += (/[\u4e00-\u9fa5]/.test(ch) ? 12 : 7);
                return w;
            }
            function nodeSize(label) {
                const PAD_X = 16, PAD_Y = 10, FONT = 12, lineH = FONT + 4;
                const rawW = estimateTextPx(label);
                let lines = [];
                if (rawW + PAD_X * 2 <= MAX_W) {
                    lines = [label];
                } else {
                    // 粗估折行
                    const halfIdx = Math.ceil(label.length / 2);
                    lines = [label.slice(0, halfIdx), label.slice(halfIdx)];
                }
                const w = Math.min(Math.max(rawW + PAD_X * 2, MIN_W), MAX_W);
                const h = PAD_Y * 2 + lineH * lines.length;
                return { width: w, height: Math.max(h, 28), lines };
            }

            // —— 递归布局：返回 { x, y, w, h, lines, children: [...], subH } ——
            function doLayout(node, depth, cursor) {
                const label = safeStr(node.label, '');
                const sz = nodeSize(label);
                const ch = node.children && node.children.length ? node.children : [];
                const x = depth * (sz.width + DX);
                if (!ch.length) {
                    return { x, y: cursor, w: sz.width, h: sz.height, lines: sz.lines, children: [], subH: sz.height };
                }
                // 先递归子节点，收集它们
                const kids = [];
                let y = cursor;
                ch.forEach(c => {
                    const sub = doLayout(c, depth + 1, y);
                    kids.push(sub);
                    y += sub.subH + NH;
                });
                const firstY = kids[0].y, lastY = kids[kids.length - 1].y;
                const myY = (firstY + lastY + (kids[kids.length - 1].h - kids[0].h)) / 2 - sz.height / 2;
                const subH = y - cursor - NH;
                return { x, y: myY, w: sz.width, h: sz.height, lines: sz.lines, children: kids, subH };
            }

            // —— 从 data 计算初始布局 ——
            let treeNode = null;
            let svgW = 0, svgH = 0;
            function computeTree() {
                const d = props.data || {};
                const root = d.root;
                if (!root) return null;
                const t = doLayout(root, 0, 0);
                // 求外框
                let maxR = 0, maxB = 0;
                function walk(n) {
                    maxR = Math.max(maxR, n.x + n.w);
                    maxB = Math.max(maxB, n.y + n.h);
                    n.children.forEach(walk);
                }
                walk(t);
                svgW = maxR + 20;
                svgH = Math.max(maxB, 28) + 20;
                return t;
            }

            // —— 扁平化树 → 便于 reactive 位置管理 ——
            const nodeId = (n, path) => path.join('.');
            function flatten(tree) {
                const all = [];
                function walk(n, path) {
                    const id = nodeId(n, path);
                    all.push({ ...n, id });
                    n.children.forEach((c, i) => walk(c, [...path, i]));
                }
                walk(tree, ['r']);
                return all;
            }
            // 重建 edges（父子连接）
            function collectEdges(tree, edges = []) {
                tree.children.forEach(c => {
                    edges.push({ from: nodeId(tree, tree._path || ['r']), to: nodeId(c, c._path) });
                    collectEdges(c, edges);
                });
                return edges;
            }

            // —— 状态 ——
            const full = ref(false);
            const zoom = ref(1);
            const panX = ref(0);
            const panY = ref(0);
            const fitMode = ref(true);
            const draggingId = ref(null);
            const dragOffset = reactive({ dx: 0, dy: 0 });
            const panning = ref(false);
            const panStart = reactive({ mx: 0, my: 0, px: 0, py: 0 });

            // —— fit 缩略图（Vue ref 精确引用，多实例不串扰）——
            const _canvasRef = ref(null);
            const _fullCanvasRef = ref(null);
            let _ro = null;
            let _fitTimer = null;   // fit 兜底重试定时器
            function getMyCanvas() {
                if (full.value) return _fullCanvasRef.value;
                return _canvasRef.value;
            }
            function fitToContainer() {
                const canvas = getMyCanvas();
                if (!canvas) return false;
                const rect = canvas.getBoundingClientRect();
                if (rect.width < 1 || rect.height < 1) return false;
                if (svgW <= 0 || svgH <= 0) return false;
                if (full.value) {
                    // 全屏：可用宽高均来自全屏画布 rect
                    const f = vizFullFit(svgW, svgH, rect.width - VIZ_FIT_PAD * 2, rect.height - VIZ_FIT_PAD * 2);
                    zoom.value = f.z; panX.value = f.panX; panY.value = f.panY;
                    return true;
                }
                // 缩略：画布高度由 vizThumbFit 产出（内容显示高 + 边距，钳 [MIN, 窗口×0.55]）
                const tf = vizThumbFit(svgW, svgH, rect.width - VIZ_FIT_PAD * 2);
                zoom.value = tf.z; panX.value = tf.panX; panY.value = tf.panY;
                canvasH.value = tf.wantH; // 同值赋值不触发重渲染，收敛无震荡
                return true;
            }
            function getCanvasEl() { return getMyCanvas(); }
            function clientToCanvas(ev) {
                const el = getMyCanvas();
                if (!el) return { x: ev.clientX, y: ev.clientY };
                const r = el.getBoundingClientRect();
                return { x: ev.clientX - r.left, y: ev.clientY - r.top };
            }
            function _scheduleFit() {
                // 多帧重试 + 定时兜底：容器未布局好（rect 无效）时避免 fit 静默失败
                if (_fitTimer) { clearInterval(_fitTimer); _fitTimer = null; }
                requestAnimationFrame(() => {
                    fitToContainer();
                    requestAnimationFrame(() => fitToContainer());
                });
                let tries = 0;
                _fitTimer = setInterval(() => {
                    if (fitToContainer() || ++tries >= 10) { clearInterval(_fitTimer); _fitTimer = null; }
                }, 120);
            }

            // 首次布局
            function initFromData() {
                treeNode = computeTree();
                if (!treeNode) return { nodes: [], edges: [] };
                function assignPath(n, path) {
                    n._path = path;
                    n.children.forEach((c, i) => assignPath(c, [...path, i]));
                }
                assignPath(treeNode, ['r']);
                const nodes = flatten(treeNode);
                const edges = collectEdges(treeNode);
                return { nodes, edges };
            }
            let { nodes: initNodes, edges: initEdges } = initFromData();
            // 缩略图画布高度（响应式）：跟随内容实际显示高（svgH×fitZoom）+ 边距，
            // 上限按窗口可视高度自适应（vizThumbFit 产出并更新）。旧实现 clamp(svgH,180,400)
            // 与容器宽度脱钩 → 宽扁图 fit 后四周大片空白。
            const canvasH = ref(Math.min(220, vizThumbMaxH()));
            const pos = reactive({});
            initNodes.forEach(n => { pos[n.id] = { x: n.x + 10, y: n.y + 10, w: n.w, h: n.h, lines: n.lines }; });

            // —— 缩放 + 平移控制（定点缩放，scale-first 模型，同 flowchart）——
            // （ZOOM_MIN/ZOOM_MAX 已提升为模块级常量，与 flowchart 共用）
            function doZoomAt(mx, my, delta) {
                fitMode.value = false;
                const oldZ = zoom.value;
                const newZ = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, +(oldZ + delta).toFixed(2)));
                if (newZ === oldZ) return;
                const ratio = newZ / oldZ;
                panX.value = +(panX.value * ratio + mx * (1 - ratio)).toFixed(2);
                panY.value = +(panY.value * ratio + my * (1 - ratio)).toFixed(2);
                zoom.value = newZ;
            }
            function resetPanZoom() { zoom.value = 1; panX.value = 0; panY.value = 0; fitMode.value = true; }
            function toolbarZoom(delta) {
                const c = getCanvasEl();
                if (!c) return doZoomAt(0, 0, delta);
                const r = c.getBoundingClientRect();
                doZoomAt(r.width / 2, r.height / 2, delta);
            }
            function onCanvasWheel(ev) {
                ev.preventDefault();
                fitMode.value = false;
                const step = ev.ctrlKey || ev.metaKey ? 0.1 : 0.06;
                const p = clientToCanvas(ev);
                doZoomAt(p.x, p.y, ev.deltaY < 0 ? step : -step);
            }
            function onCanvasMouseDown(ev) {
                if (ev.button !== 0) return;
                fitMode.value = false;
                const target = ev.target;
                const isNode = target.closest && target.closest('g');
                const canvasEl = getCanvasEl();
                const canvasRect = canvasEl ? canvasEl.getBoundingClientRect() : { left: 0, top: 0 };
                dragOffset._rect = canvasRect;
                if (!isNode) {
                    panning.value = true;
                    panStart.mx = ev.clientX - canvasRect.left;
                    panStart.my = ev.clientY - canvasRect.top;
                    panStart.px = panX.value;
                    panStart.py = panY.value;
                    ev.preventDefault();
                }
            }

            // —— 全局事件 ——
            function onMouseMove(ev) {
                const rect = dragOffset._rect || { left: 0, top: 0 };
                const cx = ev.clientX - rect.left;
                const cy = ev.clientY - rect.top;
                if (draggingId.value) {
                    pos[draggingId.value].x = (cx - panX.value) / zoom.value - dragOffset.dx;
                    pos[draggingId.value].y = (cy - panY.value) / zoom.value - dragOffset.dy;
                } else if (panning.value) {
                    panX.value = panStart.px + (cx - panStart.mx);
                    panY.value = panStart.py + (cy - panStart.my);
                }
            }
            function onMouseUp() { draggingId.value = null; panning.value = false; }
            // 窗口尺寸变化：RO 只观察画布自身（宽度变才触发），窗口高度变化需在此重算上限。
            // 手动缩放模式（fitMode=false）仅钳画布高度到新上限，不打断用户 pan/zoom 状态
            function onWinResize() {
                if (fitMode.value) { fitToContainer(); return; }
                if (!full.value) canvasH.value = Math.min(canvasH.value, vizThumbMaxH());
            }
            onMounted(() => {
                _scheduleFit();
                _ro = new ResizeObserver(() => { if (fitMode.value) fitToContainer(); });
                requestAnimationFrame(() => {
                    if (_canvasRef.value) _ro.observe(_canvasRef.value);
                });
                window.addEventListener('mousemove', onMouseMove);
                window.addEventListener('mouseup', onMouseUp);
                window.addEventListener('resize', onWinResize);
            });
            watch(full, (v) => {
                if (_ro) _ro.disconnect();
                if (v) {
                    requestAnimationFrame(() => {
                        if (_fullCanvasRef.value) { _ro.observe(_fullCanvasRef.value); _scheduleFit(); }
                    });
                } else {
                    requestAnimationFrame(() => {
                        if (_canvasRef.value) { _ro.observe(_canvasRef.value); _scheduleFit(); }
                    });
                }
            });
            onUnmounted(() => {
                window.removeEventListener('mousemove', onMouseMove);
                window.removeEventListener('mouseup', onMouseUp);
                window.removeEventListener('resize', onWinResize);
                if (_ro) { _ro.disconnect(); _ro = null; }
                if (_fitTimer) { clearInterval(_fitTimer); _fitTimer = null; }
            });

            // —— 重置 ——
            function resetPositions() {
                const fresh = computeTree();
                if (!fresh) return;
                function assignPath(n, path) {
                    n._path = path;
                    n.children.forEach((c, i) => assignPath(c, [...path, i]));
                }
                assignPath(fresh, ['r']);
                const nodes = flatten(fresh);
                nodes.forEach(n => {
                    if (pos[n.id]) {
                        pos[n.id].x = n.x + 10; pos[n.id].y = n.y + 10;
                        pos[n.id].w = n.w; pos[n.id].h = n.h; pos[n.id].lines = n.lines;
                    }
                });
            }

            // —— SVG 渲染（主视图与全屏弹窗复用）——
            function buildSvg() {
                const accentColor = 'rgba(var(--accent-rgb,120,140,255),';
                const textColor = 'rgb(var(--text-rgb,230,235,245))';
                // edges 用 from/to id 画线
                const edgePaths = initEdges.map((e, i) => {
                    const a = pos[e.from], b = pos[e.to];
                    if (!a || !b) return null;
                    const x1 = a.x + a.w, y1 = a.y + a.h / 2;
                    const x2 = b.x, y2 = b.y + b.h / 2;
                    const cx1 = x1 + DX / 2, cx2 = x2 - DX / 2;
                    return h('path', {
                        key: 'e' + i,
                        d: `M${x1},${y1} C${cx1},${y1} ${cx2},${y2} ${x2},${y2}`,
                        fill: 'none', stroke: accentColor + '.45)', 'stroke-width': 1.5
                    });
                });
                const nodeElems = Object.keys(pos).map(id => {
                    const p = pos[id];
                    return h('g', {
                        key: id,
                        transform: `translate(${p.x},${p.y})`,
                        style: { cursor: 'grab' },
                        onMousedown: (ev) => {
                            ev.preventDefault();
                            draggingId.value = id;
                            const canvasEl = getCanvasEl();
                            const canvasRect = canvasEl ? canvasEl.getBoundingClientRect() : { left: 0, top: 0 };
                            dragOffset._rect = canvasRect;
                            const cx = ev.clientX - canvasRect.left;
                            const cy = ev.clientY - canvasRect.top;
                            dragOffset.dx = (cx - panX.value) / zoom.value - p.x;
                            dragOffset.dy = (cy - panY.value) / zoom.value - p.y;
                        }
                    }, [
                        h('rect', {
                            width: p.w, height: p.h, rx: 6,
                            fill: accentColor + '.15)',
                            stroke: accentColor + '.55)',
                            'stroke-width': 1.2
                        }),
                        ...p.lines.map((line, li) => {
                            const totalLineH = p.lines.length * 16;
                            const startY = (p.h - totalLineH) / 2 + 12;
                            return h('text', {
                                x: p.w / 2, y: startY + li * 16,
                                'text-anchor': 'middle', 'font-size': 12, fill: textColor,
                                'pointer-events': 'none'
                            }, line);
                        })
                    ]);
                });
                return h('svg', {
                    width: svgW, height: svgH,
                    viewBox: `0 0 ${svgW} ${svgH}`,
                    xmlns: 'http://www.w3.org/2000/svg'
                }, [...edgePaths, ...nodeElems]);
            }

            // —— 渲染函数 ——
            return () => {
                if (!treeNode) return fallback('mindmap 缺少 root', props.raw);
                const cursor = draggingId.value ? 'grabbing' : (panning.value ? 'grabbing' : 'grab');
                const panStyle = { transform: `translate(${panX.value}px, ${panY.value}px)` };
                const vpStyle = { zoom: zoom.value };
                return h('div', { class: 'viz-mind viz-root' }, [
                    // 工具栏（与 flowchart 一致）
                    h('div', { class: 'viz-flow-bar' }, [
                        h('div', { class: 'viz-flow-actions' }, [
                            h('button', { class: 'viz-flow-btn', onClick: () => { resetPositions(); fitMode.value = true; fitToContainer(); } }, '↺ 重置'),
                            h('button', { class: 'viz-flow-btn', onClick: () => toolbarZoom(-0.1) }, '−'),
                            h('span', { class: 'viz-flow-zoom-pct' }, Math.round(zoom.value * 100) + '%'),
                            h('button', { class: 'viz-flow-btn', onClick: () => toolbarZoom(0.1) }, '+'),
                            h('button', { class: 'viz-flow-btn viz-flow-btn-primary', onClick: () => { full.value = true; fitMode.value = true; } }, '⛶')
                        ])
                    ]),
                    h('div', {
                        ref: _canvasRef,
                        class: 'viz-flow-canvas viz-mind-canvas',
                        style: { cursor, height: canvasH.value + 'px' },
                        onWheel: onCanvasWheel,
                        onMousedown: onCanvasMouseDown
                    }, [
                        h('div', { class: 'viz-pan-layer', style: panStyle }, [
                            h('div', { class: 'viz-viewport', style: vpStyle }, [buildSvg()])
                        ])
                    ]),
                    // 全屏弹窗
                    full.value ? h('div', {
                        class: 'viz-flow-full',
                        onWheel: onCanvasWheel,
                        onClick: (e) => { if (e.target.classList.contains('viz-flow-full')) full.value = false; }
                    }, [
                        h('div', { class: 'viz-flow-full-bar' }, [
                            h('div', { style: 'flex:1' }),
                            h('div', { class: 'viz-flow-full-zoom' }, [
                                h('button', { class: 'viz-flow-btn', onClick: () => toolbarZoom(-0.1) }, '−'),
                                h('span', { class: 'viz-flow-zoom-pct' }, Math.round(zoom.value * 100) + '%'),
                                h('button', { class: 'viz-flow-btn', onClick: () => toolbarZoom(0.1) }, '+'),
                                h('button', { class: 'viz-flow-btn viz-flow-btn-reset', onClick: resetPanZoom }, '1:1'),
                                h('button', { class: 'viz-flow-btn viz-flow-btn-close', onClick: () => { full.value = false; } }, '关闭 ✕')
                            ])
                        ]),
                        h('div', {
                            ref: _fullCanvasRef,
                            class: 'viz-flow-full-canvas',
                            style: { cursor },
                            onMousedown: onCanvasMouseDown
                        }, [
                            h('div', { class: 'viz-pan-layer', style: panStyle }, [
                                h('div', { class: 'viz-viewport', style: vpStyle }, [buildSvg()])
                            ])
                        ])
                    ]) : null
                ]);
            };
        }
    });

    // ============ html ============
    // 预注入的 base CSS：滚动/溢出由注入脚本按「自适应内容 + 对话区上限」动态接管，
    // 用户自定义容器的 overflow 不受影响
    // 缩略图和全屏弹窗都用这一套
    const IFRAME_BASE_CSS = `<style>
html{margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;font-size:13px;color:#e6ebf5;background:#0a0e1a;overflow:hidden;}
body{margin:0;padding:8px;}
/* 仅给用户自定义的 overflow 容器美化滚动条（html 层滚动由脚本动态开/关）*/
*,*::before,*::after{scrollbar-width:thin;scrollbar-color:rgba(157,180,255,0.4) transparent;}
*::-webkit-scrollbar{width:4px;height:4px;}
*::-webkit-scrollbar-track{background:transparent;}
*::-webkit-scrollbar-thumb{background:rgba(157,180,255,0.4);border-radius:3px;}
*::-webkit-scrollbar-thumb:hover{background:rgba(157,180,255,0.7);}
*::-webkit-scrollbar-thumb:active{background:rgba(157,180,255,0.9);}
/* —— range slider 小巧美化 —— */
input[type=range]{-webkit-appearance:none;appearance:none;background:transparent;cursor:pointer;outline:none;padding:0;margin:4px 0;}
input[type=range]::-webkit-slider-runnable-track{height:3px;border-radius:2px;background:rgba(120,140,255,0.15);}
input[type=range]::-moz-range-track{height:3px;border-radius:2px;background:rgba(120,140,255,0.15);border:none;}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:10px;height:10px;border-radius:50%;background:#9db4ff;box-shadow:0 1px 3px rgba(0,0,0,0.35);margin-top:-3.5px;transition:transform .1s;}
input[type=range]::-webkit-slider-thumb:hover{transform:scale(1.2);}
input[type=range]::-webkit-slider-thumb:active{transform:scale(1.05);}
input[type=range]::-moz-range-thumb{width:10px;height:10px;border-radius:50%;background:#9db4ff;box-shadow:0 1px 3px rgba(0,0,0,0.35);border:none;}
input[type=range].viz-vertical{writing-mode:vertical-lr;direction:rtl;height:80px;}
input[type=range].viz-vertical::-webkit-slider-runnable-track{width:3px;height:100%;}
input[type=range].viz-vertical::-webkit-slider-thumb{margin-top:0;margin-left:-3.5px;}
</style>`;
    // 自适应测量脚本（注入 iframe 内运行）：
    // 1. 宽度 fit：内容自然宽超出视口时对 body 应用 CSS zoom 等比缩小（布局级缩放，字体清晰），
    //    zoom 下限 0.3（与 flowchart ZOOM_MIN 一致），仍超宽则开横向滚动（内容不裁剪）
    // 2. 高度实测：body.scrollHeight（含 zoom 后布局值）postMessage 上报给父页，
    //    由父页按「内容高度 vs 对话区上限」设定 iframe 高度
    // 3. 滚动自判：内容高超过 iframe 视口高时 html 纵向滚动（超上限时内容完整可达）
    // 4. ResizeObserver 持续监听：内容动态变化（JS 生成/展开折叠）、视口变化都重新自适应
    const IFRAME_FIT_SCRIPT = `<script>(function () {
var MINZ = 0.3, T = 0;
function measure() {
    try {
        var d = document, de = d.documentElement, b = d.body;
        if (!b) return;
        // 清 zoom 测自然宽（同一次任务内完成，无渲染帧，不闪烁）
        b.style.zoom = '';
        var vw = window.innerWidth || (de && de.clientWidth) || 0;
        var nw = Math.max(b.scrollWidth, (de && de.scrollWidth) || 0);
        var r = 1;
        if (nw > vw + 1) r = Math.max(MINZ, vw / nw);
        b.style.zoom = (r < 1) ? r : '';
        var h = b.scrollHeight + 2;
        var visW = Math.ceil(nw * r);
        if (de) {
            de.style.overflowX = (visW > vw + 2) ? 'auto' : 'hidden';
            de.style.overflowY = (h > (window.innerHeight || 0) + 2) ? 'auto' : 'hidden';
        }
        parent.postMessage({ __vizHtmlSize: 1, w: visW, h: h }, '*');
    } catch (e) { /* 沙箱异常静默 */ }
}
function sched() { if (T) return; T = setTimeout(function () { T = 0; measure(); }, 80); }
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { setTimeout(measure, 0); });
} else { setTimeout(measure, 0); }
window.addEventListener('load', function () { setTimeout(measure, 0); });
window.addEventListener('resize', sched);
setTimeout(measure, 300); // 初帧兜底
function bindRO() {
    if (!window.ResizeObserver || !document.body) return;
    try {
        var ro = new ResizeObserver(sched);
        ro.observe(document.documentElement);
        ro.observe(document.body);
    } catch (e) { /* RO 不可用时靠事件兜底 */ }
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bindRO);
else bindRO();
})();</script>`;
    // 把 CSS + 测量脚本注入 srcdoc 的辅助函数（注入点多级兜底：head → body 前 → html 后 → 开头拼接）
    function injectBoot(rawHtml, boot) {
        if (/<head[^>]*>/i.test(rawHtml)) return rawHtml.replace(/<head([^>]*)>/i, '<head$1>' + boot);
        if (/<body[^>]*>/i.test(rawHtml)) return rawHtml.replace(/<body([^>]*)>/i, '<body$1>' + boot);
        if (/<html[^>]*>/i.test(rawHtml)) return rawHtml.replace(/<html([^>]*)>/i, '<html$1>' + boot);
        return boot + rawHtml;
    }
    const Html = defineComponent({
        props: { data: Object, raw: String, extAPI: Object },
        setup(props) {
            const full = ref(false);
            const data = props.data || {};
            const rawHtml = safeStr(data.html, '');
            const srcdoc = injectBoot(rawHtml, IFRAME_BASE_CSS + IFRAME_FIT_SCRIPT);
            const sandbox = data.sandbox || 'allow-scripts allow-forms allow-popups';

            // —— 缩略图尺寸自适应（内容完整 + 对话区上限）——
            // iframe 高度 = min(内容实测高, 上限)；上限按对话区可视高度比例计算，
            // 超上限时 iframe 内部自动开滚动（见 IFRAME_FIT_SCRIPT），内容完整可达
            const thumbIframe = ref(null);
            const lastH = ref(0); // 最近一次上报的内容视觉高度
            function maxH() {
                const vh = (typeof window !== 'undefined' && window.innerHeight) || 800;
                return Math.max(160, Math.min(520, Math.round(vh * 0.55)));
            }
            const MIN_INIT_H = 120;
            const thumbH = ref(Math.max(MIN_INIT_H, Math.min(Number(data.height) || 200, maxH())));
            function applyH(h) {
                lastH.value = h;
                thumbH.value = Math.max(MIN_INIT_H, Math.min(h, maxH()));
            }
            function onMsg(e) {
                const d = e && e.data;
                if (!d || d.__vizHtmlSize !== 1) return;
                // e.source 精确匹配本实例的缩略 iframe（多实例不串扰；全屏 iframe 自适应自身视口无需处理）
                if (thumbIframe.value && e.source === thumbIframe.value.contentWindow) {
                    const h = Number(d.h) || 0;
                    if (h > 0) applyH(h);
                }
            }
            function onWinResize() {
                // 对话区尺寸变化 → 重算上限（内容高度不变）
                if (lastH.value) thumbH.value = Math.max(MIN_INIT_H, Math.min(lastH.value, maxH()));
            }
            onMounted(() => {
                window.addEventListener('message', onMsg);
                window.addEventListener('resize', onWinResize);
            });
            onUnmounted(() => {
                window.removeEventListener('message', onMsg);
                window.removeEventListener('resize', onWinResize);
            });
            return () => h('div', { class: 'viz-html viz-root' }, [
                h('div', { class: 'viz-html-bar' }, [
                    h('div', { style: 'flex:1' }),
                    h('button', { class: 'viz-html-zoom', onClick: () => { full.value = true; } }, '放大')
                ]),
                h('iframe', { ref: thumbIframe, srcdoc, style: { height: thumbH.value + 'px' }, sandbox }),
                full.value ? h('div', { class: 'viz-html-full', onClick: (e) => { if (e.target.classList.contains('viz-html-full')) full.value = false; } }, [
                    h('div', { class: 'viz-html-full-bar' }, [
                        h('div', { style: 'flex:1' }),
                        h('button', { class: 'viz-html-close', onClick: () => { full.value = false; } }, '关闭 ✕')
                    ]),
                    h('iframe', { srcdoc, sandbox })
                ]) : null
            ]);
        }
    });

    // ============ layout ============
    // 优化排布：多列网格，每项递归派发对应渲染器
    const Layout = defineComponent({
        props: { data: Object, raw: String, extAPI: Object },
        setup(props) {
            return () => {
                const d = props.data || {};
                const cols = Math.max(1, Math.min(6, Number(d.columns) || 2));
                const items = Array.isArray(d.items) ? d.items : [];
                return h('div', { class: 'viz-layout viz-root', style: { gridTemplateColumns: `repeat(${cols}, 1fr)` } },
                    items.map(it => {
                        const type = safeStr(it.type, '');
                        const comp = REGISTRY[type];
                        const child = comp
                            ? h(comp, { data: it.data || {}, raw: JSON.stringify(it.data || {}), extAPI: props.extAPI })
                            : fallback('未知子类型: ' + type, JSON.stringify(it.data || {}));
                        return h('div', { class: 'viz-cell' }, [
                            it.title ? h('div', { class: 'viz-cell-title' }, safeStr(it.title, '')) : null,
                            child
                        ]);
                    })
                );
            };
        }
    });

    // —— 注册 ——
    register('choice', Choice);
    register('fill', Fill);
    register('image', Image_);
    register('flowchart', Flowchart);
    register('mindmap', Mindmap);
    register('html', Html);
    register('layout', Layout);

    console.info('[visual_interaction] 已注册渲染器: choice, fill, image, flowchart, mindmap, html, layout');
})();