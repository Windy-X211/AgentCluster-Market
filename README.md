# AgentCluster 市场资源仓库

智能体集群（AgentCluster）的**独立市场仓库**，与主源码仓库分离：

- 主源码仓库：`AgentCluster-Project`（只含代码，不跟踪 `market/`）
- 市场仓库：`AgentCluster-Market`（本仓库，只含可下载 / 可发布的资源）

主程序的 `market/` 目录就是本仓库的 git 工作副本。打包时该目录会作为
`extraResources` 内置进安装包，因此离线环境下也能浏览和安装已有资源。

## 目录结构

```
market/
  index.json                 统一索引（所有资源的元数据，发布时增量更新）
  agents/<slug>/agent.json
  canvases/<slug>/canvas.json
  backgrounds/<id>/manifest.json + 资源文件
  skills/<id>/SKILL.md + 辅助文件
  plugins/<id>/manifest.yaml + 代码
  extensions/<id>/manifest.json + 代码
```

## 下载（普通用户）

主程序市场页 / 独立市场管理程序点击「同步最新」，按配置顺序自动尝试镜像，
全部失败后回退 GitHub 官方地址：

1. `https://ghfast.top/{url}`
2. `https://gh-proxy.com/{url}`
3. GitHub 官方

镜像模板、仓库地址、分支可在「市场 → 设置」里修改（保存在用户数据目录的
`settings.json`：`market_repo_url` / `market_mirrors` / `market_branch`）。

同步只做「新增 + 更新」，不会删除本地条目；本地已发布但尚未推送的改动会
被保护，不会被远端内容覆盖。

## 上传（发布者）

1. 在市场管理程序里「发布到市场」（写入本地 `market/` 与索引）；
2. 勾选「发布后推送到远程仓库」，或点「推送远程」。

推送走**官方仓库地址**：GitHub 加速镜像只代理读取，不支持 `git push`。
首次推送前需要先在 GitHub 建好空仓库，并保证本机 git 凭据有写权限。

命令行等价操作：

```bash
cd market
git add -A
git commit -m "publish agent/xxx"
git push -u origin main
```

## 贡献约定

- 资源 id 只使用英文、数字、下划线、短横线；
- 画布发布时必须内联节点引用的智能体快照（主程序已自动处理）；
- 索引使用增量写入：只增加 / 更新本次条目，不要整体重写 `index.json`。