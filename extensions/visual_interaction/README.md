# 可视化交互拓展 (visual_interaction)

解析智能体输出的 JSON，渲染为富交互组件。

## 支持的可视化类型

| type | 说明 |
|------|------|
| `choice` | 选择题，先选择再点「提交」按钮发送 |
| `fill` | 填空题，输入后点「提交」按钮发送（Enter 也提交） |
| `image` | 配图，可放大 |
| `flowchart` | 流程图（分层 SVG）；缩略图按对话区自适应尺寸（内容显示高 + 边距，超限拖拽/全屏查看） |
| `mindmap` | 思维导图（横向树）；缩略图按对话区自适应尺寸，同 flowchart |
| `html` | HTML 缩略卡片，可交互，可放大全屏；缩略图自适应内容尺寸并按对话区限制（超宽等比缩小、超高内部滚动，内容完整） |
| `layout` | 优化排布，多列网格嵌套任意 viz |

## 触发方式

智能体在对话中输出 markdown 代码块：

~~~
```viz:choice
{"question": "请选择方案", "options": ["A", "B", "C"]}
```
~~~

或裸 JSON：

```json
{"__viz__": {"type": "choice", "data": {"question": "...", "options": [...]}}}
```

## 示例

### 选择题（单选）
~~~
```viz:choice
{"question": "请选择部署方案", "options": ["方案A：云端", "方案B：本地", "方案C：混合"]}
```
~~~

### 选择题（多选）
~~~
```viz:choice
{"question": "需要哪些能力？", "multiple": true,
 "options": [{"label": "代码生成", "value": "codegen"}, {"label": "文档写作", "value": "doc"}]}
```
~~~

### 填空题（多空）
~~~
```viz:fill
{"question": "请填写个人信息",
 "blanks": [
   {"label": "姓名", "placeholder": "请输入姓名"},
   {"label": "年龄", "placeholder": "请输入年龄", "type": "number"}
 ]}
```
~~~

### 填空题（单空简写）
~~~
```viz:fill
{"question": "你的城市是？", "label": "城市", "placeholder": "如：北京"}
```
~~~

### 流程图
~~~
```viz:flowchart
{"nodes":[{"id":"a","label":"开始"},{"id":"b","label":"处理"},{"id":"c","label":"结束"}],
"edges":[{"from":"a","to":"b"},{"from":"b","to":"c"}]}
```
~~~

### 思维导图
~~~
```viz:mindmap
{"root":{"label":"项目","children":[{"label":"前端","children":[{"label":"Vue"},{"label":"Vite"}]},{"label":"后端"}]}}
```
~~~

### 优化排布
~~~
```viz:layout
{"columns":2,"items":[{"type":"choice","title":"方案选择","data":{"question":"选哪个？","options":["A","B"]}},
{"type":"image","title":"示意图","data":{"url":"https://example.com/a.png"}}]}
```
~~~