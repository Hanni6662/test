# 图片消息通信兼容补丁

适用范围仅为 OpenAI API Compatible 0.0.68 指定包、模型 `claude-sonnet-5-5`、网关 `api.aipaibox.com`。该网关在本次对照中未读取独立用户图片消息；适配器合并相邻的含图片用户消息，保留所有内容和顺序，不跨 system/assistant/tool。其他模型、其他网关与纯文本请求保持原样。补丁不修改 Dify 节点、提示词、连线、表单、发布版本或凭证。

插件安装包和工作区凭证不随交付包提供。先在接收者的 Dify 安装对应官方插件，并配置自己的模型凭证。安装器仅接受已核对的包目录和 llm.py 哈希；遇到其他版本或已有自定义代码会拒绝覆盖，须先重新评估兼容性。

在开发包根目录使用 Python 3.12 执行，下面备份路径须换为自己机器上位于交付目录、插件存储目录之外的私有目录：

```powershell
python './chatflow/provider-compat/install_vision_message_compat.py' --plugin-root './chatflow/volumes/plugin_daemon/cwd/langgenius' --backup-dir '<自己的私有补丁备份目录>' --check-only
python './chatflow/provider-compat/install_vision_message_compat.py' --plugin-root './chatflow/volumes/plugin_daemon/cwd/langgenius' --backup-dir '<自己的私有补丁备份目录>'
docker compose --env-file './chatflow/.env' -f './chatflow/compose.yaml' restart plugin_daemon
```

须在没有进行中的模型调用时重启接收者自己的 plugin_daemon，保留其其他运行 overlay。安装器重复执行不会覆盖原始备份。Windows/Docker Desktop 默认绑定存储路径如上；采用其他 volume 或 Compose 布局时，先找到实际 storage/cwd/langgenius 挂载目录再传入。

安装器无网络回归：`python -m unittest discover -s './chatflow/provider-compat' -p 'test_installer.py' -v`。

真实 SDK 无网络回归需使用该插件安装目录 `.venv/bin/python`，将此工具目录挂载到插件容器的 `/verify` 后执行：

```sh
PYTHONPATH=/verify /app/storage/cwd/langgenius/openai_api_compatible-0.0.68@ba61c5eccb814be7364117c0bec3710a51e09b20f31e37243e747a00f9377969/.venv/bin/python /verify/test_vision_message_compat.py
```

不要复制现有工作区凭证或整个 plugin_daemon volume 到开发包。升级或重装插件会覆盖补丁，需重新检查版本、源码哈希及识图结果。

回退：停止新的模型调用，恢复私有备份中的 `provider-llm-before.py` 到对应 `models/llm/llm.py`，重启接收者的插件守护进程。辅助模块可保留，原始 llm.py 不再导入它。无需重新发布主流程。
