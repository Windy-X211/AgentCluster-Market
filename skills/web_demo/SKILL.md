---
name: web_demo
label: Web 演示页面生成
description: 生成美观、响应式、交互性强的完整 HTML/CSS/JS 网页演示。当用户说"演示网页""做个网页""前端演示""HTML 页面""落地页""展示页面""交互效果""动画效果"或任何包含"html""demo""网页"的请求时加载此技能。
version: "1.1.0"
author: AgentCluster
tags: [web, html, css, javascript, demo, frontend]
---

# Web 演示页面生成

## 何时使用

当用户想要一个**完整可运行的网页**——包含布局、样式、动画和交互——时使用此技能。

适用场景：
- 快速生成产品落地页 / 展示页 / 个人主页
- 前端效果演示（动画、轮播、滚动效果、表单交互等）
- 原型可视化（把设计想法变成可点击的页面）
- 教学/展示用途的 HTML 演示

**不要用在**：
- 需要后端逻辑、数据库、用户认证的完整 Web 应用 → 建议用户使用项目脚手架
- 纯文字内容排版（用 Markdown 即可）
- 已有框架项目（Vue/React）中新增组件

## 工作流程

按以下顺序与用户协作，每一步完成后简要确认再进入下一步：

### 第 1 步：需求澄清

向用户确认以下信息（用户未明确时主动询问）：

- **页面类型**：落地页 / 展示页 / 仪表盘 / 表单页 / 动画演示 / 其他
- **主题与风格**：科技感 / 简约 / 商务 / 活泼 / 暗色系 …
- **核心内容**：需要展示哪些区块（Hero、功能介绍、价目表、联系方式…）
- **交互需求**：有无动画、轮播、表单提交、滚动效果等
- **输出方式**：单文件 HTML（内联 CSS/JS）还是分离文件（index.html + style.css + script.js）

> 若用户赶时间或要求"直接给我看效果"，跳过确认，用合理默认值一次性生成。

### 第 2 步：结构设计

在脑中规划页面结构，快速口述（1-3 句）：
- 页面分几个区块，每个区块的布局方式（Flex / Grid / 居中）
- 主色调与辅助色
- 关键交互点

不需要输出设计文档，但确保心中有数再写代码。

### 第 3 步：生成代码

根据输出方式生成完整代码：

#### 模式 A：单文件输出（默认，适合快速演示）

输出一个完整的 `index.html`，CSS 用 `<style>` 内联，JS 用 `<script>` 内联。用户保存后双击即可在浏览器打开。

#### 模式 B：分离文件输出（适合较大项目）

输出三个文件：
- `index.html` — 语义化 HTML 结构
- `style.css` — 完整样式
- `script.js` — 交互逻辑

用户保存到同一目录即可运行。

### 第 4 步：自检与交付

生成代码后，自检以下要点：
- [ ] HTML 语义化：用 `<header>` `<nav>` `<main>` `<section>` `<footer>` 而非全 `<div>`
- [ ] 响应式：包含 `viewport` meta，在手机/平板/桌面下不溢出
- [ ] 无障碍：图片有 `alt`，表单有 `label`，按钮有可读文本
- [ ] 性能：CSS 在 `<head>`，JS 在 `</body>` 前，无阻塞渲染
- [ ] 安全：无内联事件处理器（`onclick=`），无 `eval()`，无外部不可信资源
- [ ] 无硬编码外部依赖（CDN 链接需注明可替换）

自检通过后，用以下格式交付：

```
📁 项目结构
  index.html（或 index.html + style.css + script.js）

🔗 预览方式
  将文件保存到本地，双击 index.html 即可在浏览器中打开预览。

📝 自检结果
  ✅ 响应式布局  ✅ 语义化 HTML  ✅ 无障碍标签  ✅ 性能优化
```

## 代码质量规范

### HTML
- 使用语义化标签：`<header>`, `<nav>`, `<main>`, `<section>`, `<article>`, `<aside>`, `<footer>`
- 所有图片加 `alt` 属性，装饰性图片用 `alt=""`
- 表单元素配 `<label>`，用 `for` 关联
- `<meta name="viewport">` 必须存在

### CSS
- 优先使用 CSS 变量定义主题色，方便用户改色：

```css
:root {
  --primary: #4f46e5;
  --primary-dark: #4338ca;
  --bg: #ffffff;
  --text: #1f2937;
  --text-muted: #6b7280;
  --radius: 0.5rem;
  --shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
}
```

- 布局优先 Flexbox / Grid，避免 float
- 响应式用 `clamp()`、`min()`、`max()` 和媒体查询
- 动画优先 CSS `transition` / `@keyframes`，轻量场景不用 JS
- 避免使用 `!important`

### JavaScript
- 使用原生 ES6+，无需引入框架（除非用户要求）
- 事件绑定用 `addEventListener`，不用内联 `onclick`
- DOM 操作在 `DOMContentLoaded` 后执行
- 交互效果示例：滚动高亮导航、轮播、模态框、手风琴、表单验证

## 代码示例（单文件模式）

以下是一个完整的落地页示例，展示上述所有规范：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>产品落地页</title>
  <style>
    :root {
      --primary: #4f46e5;
      --primary-dark: #4338ca;
      --bg: #ffffff;
      --bg-alt: #f9fafb;
      --text: #1f2937;
      --text-muted: #6b7280;
      --radius: 0.5rem;
      --shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
    }
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      line-height: 1.6;
      color: var(--text);
      background: var(--bg);
    }

    /* 导航栏 */
    .navbar {
      position: fixed; top: 0; width: 100%;
      background: rgba(255, 255, 255, 0.9);
      backdrop-filter: blur(8px);
      z-index: 100;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.05);
    }
    .navbar ul {
      display: flex; justify-content: center; gap: 2rem;
      list-style: none; padding: 1rem;
    }
    .navbar a {
      color: var(--text); text-decoration: none; font-weight: 500;
      transition: color 0.2s;
    }
    .navbar a:hover, .navbar a.active { color: var(--primary); }

    /* Hero 区 */
    .hero {
      min-height: 100vh; display: flex; flex-direction: column;
      align-items: center; justify-content: center;
      text-align: center; padding: 2rem;
      background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
      color: #fff;
    }
    .hero h1 { font-size: clamp(2rem, 5vw, 3.5rem); margin-bottom: 1rem; }
    .hero p { font-size: clamp(1rem, 2vw, 1.25rem); margin-bottom: 2rem; opacity: 0.9; }
    .btn-primary {
      padding: 0.75rem 2rem; background: #fff; color: var(--primary);
      border: none; border-radius: var(--radius); font-size: 1rem;
      font-weight: 600; cursor: pointer; transition: transform 0.2s, box-shadow 0.2s;
    }
    .btn-primary:hover { transform: translateY(-2px); box-shadow: var(--shadow); }

    /* 功能区 */
    .features {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 2rem; padding: 4rem 2rem; max-width: 1200px; margin: 0 auto;
    }
    .feature-card {
      background: var(--bg-alt); padding: 2rem; border-radius: var(--radius);
      box-shadow: var(--shadow); transition: transform 0.3s;
    }
    .feature-card:hover { transform: translateY(-4px); }
    .feature-card h3 { color: var(--primary); margin-bottom: 0.5rem; }

    /* 页脚 */
    .footer {
      background: var(--text); color: #fff; text-align: center;
      padding: 2rem;
    }

    @media (max-width: 600px) {
      .navbar ul { gap: 1rem; flex-wrap: wrap; }
    }
  </style>
</head>
<body>
  <header>
    <nav class="navbar">
      <ul>
        <li><a href="#hero">首页</a></li>
        <li><a href="#features">功能</a></li>
        <li><a href="#footer">联系</a></li>
      </ul>
    </nav>
  </header>

  <main>
    <section id="hero" class="hero">
      <h1>欢迎使用产品</h1>
      <p>简单、优雅、强大的解决方案</p>
      <button class="btn-primary" onclick="document.getElementById('features').scrollIntoView({behavior:'smooth'})">了解功能</button>
    </section>

    <section id="features" class="features">
      <article class="feature-card">
        <h3>⚡ 高性能</h3>
        <p>毫秒级响应，轻松应对高并发场景。</p>
      </article>
      <article class="feature-card">
        <h3>🔒 安全可靠</h3>
        <p>企业级安全加密，数据保护万无一失。</p>
      </article>
      <article class="feature-card">
        <h3>📱 全平台</h3>
        <p>一次开发，桌面、移动、平板完美适配。</p>
      </article>
    </section>
  </main>

  <footer class="footer">
    <p>&copy; 2026 示例公司 · 仅供演示</p>
  </footer>

  <script>
    // 滚动高亮导航
    const sections = document.querySelectorAll('section[id], footer[id]');
    const navLinks = document.querySelectorAll('.navbar a');
    const observer = new IntersectionObserver((entries) => {
      entries.forEach(entry => {
        const link = document.querySelector(`.navbar a[href="#${entry.target.id}"]`);
        if (!link) return;
        link.classList.toggle('active', entry.isIntersecting);
      });
    }, { rootMargin: '-50% 0px -50% 0px' });
    sections.forEach(sec => observer.observe(sec));
  </script>
</body>
</html>
```

## 可选扩展

- **动画增强**：引入 [AOS](https://unpkg.com/aos@2.3.1)（滚动动画）、[GSAP](https://cdnjs.com/gsap)（复杂动画序列），通过 CDN `<script>` 标签引入
- **图标库**：[Lucide](https://lucide.dev) / [Font Awesome](https://cdnjs.com/font-awesome) 提供矢量图标
- **字体**：[Google Fonts](https://fonts.google.com) 提供中英文字体
- **图表**：[Chart.js](https://cdn.jsdelivr.net/npm/chart.js) 轻量数据可视化
- **CSS 框架**：若用户要求快速布局，可用 [Tailwind CSS](https://cdn.tailwindcss.com)（CDN 版，仅适合演示）

> 引入 CDN 资源时，在交付说明中标注「需联网才能正常显示」。

## 安全与边界

- 不嵌入恶意脚本（挖矿、窃取数据、弹窗轰炸等）
- 不使用 `eval()`、`document.write()`、内联 `onclick`（示例中的 onclick 仅作极简演示，正式代码改用 `addEventListener`）
- CDN 链接只使用可信来源（unpkg、cdnjs、jsdelivr、google fonts）
- 不收集用户输入发送到外部服务器
- 演示页面不包含真实联系方式、真实公司信息
- 大型动画优先用 CSS 实现，JS 只做逻辑控制，避免性能问题

## 交付后

生成完成后主动问用户：
- 要不要换配色 / 改布局 / 加区块？
- 要不要加动画效果（滚动渐入、轮播、计数器等）？
- 要不要导出分离文件版本？
- 要不要加暗色模式切换？