# GitHub 三人协作分支设计

## 目标

将当前本地项目连接到私有仓库 `https://github.com/Hanni6662/test`，并在同一个仓库中建立适合三人并行开发的分支层次。此次工作只设置 Git 分支和协作约定，不重构现有代码目录。

## 分支层次

- `main`：稳定分支，只接收已经在本地完成组装确认的版本。
- `integration`：集成分支，由项目负责人合并三方工作并检查能否运行。
- `dev/frontend`：前端开发者的长期开发分支。
- `dev/backend`：后端开发者的长期开发分支。
- `dev/agent`：Agent 开发及项目集成负责人的长期开发分支。

所有分支从同一个当前基线提交创建，避免首次协作时出现无关历史差异。

## 合并方向

```text
dev/frontend --\
dev/backend  ----> integration ----> main
dev/agent    --/
```

开发者不直接向 `main` 推送。三方修改先进入各自 `dev/*` 分支，再由项目负责人合并到 `integration`。只有 `integration` 在负责人电脑上确认可以运行后，才合并到 `main`。

如果集成时发现问题，修复应回到产生问题的 `dev/*` 分支完成，再重新合入 `integration`，避免只在集成分支修复而造成分支内容不一致。

## 接口契约约定

前端、后端与 Agent 共享同一套接口字段、枚举、错误码和 JSON 示例。当前阶段以仓库内共同评审的契约文档或现有类型文件为准；任何一方需要修改接口时，必须先让另外两方确认，再分别适配各自模块。

后续进行代码分层时，再将权威契约集中到 `packages/contracts`。本次分支设置不提前移动现有 `src` 代码，也不引入新的构建配置。

## GitHub 可见性和权限

仓库保持 Private，只保留仓库所有者和已经邀请的两位协作者。由于当前使用免费方案且不配置付费分支保护，团队通过约定保证只有项目负责人执行 `integration -> main` 的最终合并。

真实 API Key、密码、Dify Key 和其他密钥不得提交到任何分支。

## 完成标准

- 本地远程地址指向 `https://github.com/Hanni6662/test.git`。
- GitHub 上存在 `main`、`integration`、`dev/frontend`、`dev/backend`、`dev/agent`。
- 五个分支初始时指向同一个已确认的项目基线。
- 当前未跟踪文件和现有用户改动不因分支设置被删除或覆盖。
