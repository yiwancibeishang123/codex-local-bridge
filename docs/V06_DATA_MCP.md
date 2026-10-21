# Codex Local Bridge v0.6（MCP-first）· 科研大数据访问

## 为什么从桌面回到 MCP

用户实机反馈：AirGym 包含 ROSbag、点云、训练轨迹和模型文件，原有 `read_project_file` 只接受白名单 UTF-8 文件，单次最大 128 KiB。**即使整个项目授予了普通代码读写，也不代表所有二进制数据已获得读取授权。** 桌面应用状态卡片也不应成为权限更改的前提。

v0.6 把**科研数据权限**从普通代码权限中分离，直接提供 ChatGPT MCP 工具：选择数据类型、授权目录、查询当前状态、列出已授权数据、读取分块文本或在 Ubuntu 本地处理数据并返回紧凑统计。所有数据仍保存在 Ubuntu 的用户目录，不复制到 GitHub、不自动运行训练脚本。

## 在 ChatGPT 对话里如何使用

1. 对话中说 **“列出 airgym_yingjie 可以申请访问的科研数据类型”**：调用 `list_data_profiles(project_id)`。
2. 比如想查看整个 AirGym：说 **“为 airgym_yingjie 的所有子目录申请 ros / pointcloud / arrays / logs / images / documents / models / configuration 的读取权限”**。工具调用 `request_data_access(project_id, profiles=[...], directory=".", access="read")`，返回一次性申请编号。
3. 为了避免远程会话擅自扩大数据访问范围，**申请只进入待批准状态**，此时 ChatGPT 仍不能读到新类别。由当前 Ubuntu 登录用户在本机确认：
   `codex-local-bridge approve-data-scope <申请编号>`，按提示输入明确授权短语。CLI 会展示将要授权的项目、目录、格式及读/写级别。
4. **授权即时生效，后续不需要重启 MCP、重新登录或刷新桌面 UI**。在当前对话里调用 `get_project_data_access` 或 `get_data_access_request` 即可查看实时状态。
5. `list_project_data_files(project_id, relative_dir, offset, limit)` 列出数据文件（含大小）、允许分页及子目录。
6. `inspect_project_data(project_id, relative_path, max_rows)` 只在 Ubuntu 本机读取一部分内容，向 ChatGPT 返回有限结构化结果。超过 128KiB 的 CSV、NPY 等仍可统计/查看头部信息。
7. 对较大的日志或 CSV，需要按字节位置读取文本时调用 `read_project_data_chunk`，单次最多 16 KiB，并进行凭据敏感内容检测。

**新增接口**：`list_data_profiles`、`get_project_data_access`、`request_data_access`、`get_data_access_request`、`list_project_data_files`、`inspect_project_data`、`read_project_data_chunk`、`request_data_text_write`。原有 `list_projects`、`read_project_file`、`request_edit_file`、`delegate_task` 等全部保留。

首次升级后，ChatGPT 可能需要重新加载/重新发现 MCP 工具列表以显示新增的工具。**权限动态生效不等于插件工具清单动态更新**：前者无需重启，后者取决于当前 ChatGPT 会话的连接发现。

## 支持哪些科研文件？

| 数据类型 | 常见扩展名 | 当前读取方式 |
|---|---|---|
| ros | .db3 / .bag / .mcap | .db3 识别 ROSbag2 SQLite，获取 topics 和少量消息时间戳；.bag/.mcap 只返回安全元信息 |
| pointcloud | .pcd / .ply / .las / .laz / .bin | PCD/PLY 头字段；其余文件大小、前 32 字节签名，不解码完整点云 |
| arrays | .npy / .npz / .csv / .tsv / .parquet / .feather / .h5 / .hdf5 | NPY shape/dtype、NPZ 容器成员元信息、CSV/TSV 有界样本及基础数值统计；Parquet/HDF5 等目前只有元信息 |
| logs | .jsonl / .log / .txt / .csv / .tsv / .json | JSONL 字段/有界样本、CSV 统计或文本按偏移分块 |
| images | .png/.jpg/.tif 等 | 文件元信息（当前不会把原图当作可视化内容返回） |
| documents | .pdf/.docx/.xlsx/.pptx | 文件元信息（没有绕过已有专用文档解析工具） |
| models | .pt/.pth/.onnx/.safetensors/.ckpt | 仅安全元信息；**不执行 torch.load、pickle 或模型代码** |
| configuration | .yaml/.yml/.toml/.xml/.ini/.cfg | UTF-8 文本分块，不修改配置 |

安全的统计/抽样有硬上限：CSV/JSONL 本地最多采样约 2000 行、4 MiB，最多返回 20 行样例；单次文本分块最多 16 KiB。它们是**样本统计而非整份数据的精确全量统计**，结果会标注截断。ROSbag 的 CDR 二进制消息不会在此版本自动反序列化。大规模 ROS 数据精确解码与分布统计需要专用读取器，计划作为后续可选模块。

## 读取与写入的区别

选择 `access="read"` 后，仅允许列目录、取部分文本、分析数据元信息。**整个 AirGym 目录**指项目根目录下经批准的*这些文件格式*，不是主目录、系统目录、隐藏凭据、符号链接或任意扩展名。

选择 `access="write_review"` 前要求项目已经获本机正式读写授权，并且选中的 profile 包含可写的文本格式。即使获得这一权限，`request_data_text_write` 也只是生成变更提案，**绝不因普通代码的自动写回授权而直接覆盖数据**。仅支持不超过 128KiB 的有限 UTF-8 CSV/TSV/JSON/JSONL/TXT/LOG 文件；必须给出当前文件 SHA-256 或 `ABSENT`；本机逐次执行 `codex-local-bridge approve-data-write <提案编号>` 后，才会再次校验旧 SHA 并创建备份、原子写入。

对 `.db3`、`.bag`、`.pcd`、`.npy`、`.pt`、`.pth` 等二进制数据**没有远程直接覆写接口**。分析完成后若需改变这些数据，应在本机使用适配格式的工具重新生成衍生文件，并保留原始数据可追溯性。

撤销全部新增科研数据授权：本机执行 `codex-local-bridge revoke-data-scope <项目ID>`；立即生效。代码读写授予和科研数据读取授权独立管理，不必重置现有 AirGym 代码授权或 OAuth/Tailscale 登录。

## 安全与隐私界限

- 不能从 ChatGPT 网页远程批准新的数据目录/格式，也不能靠编辑项目文件、提示词或模型输出扩大授权。
- `request_data_access` 不修改 root、include 或 systemd 配置；授权记录保存到已有用户私有 SQLite，每次读取时重新检查项目 root 的 inode/设备号。
- Linux 目录/file-descriptor no-follow、常见凭据文件名阻断、普通文件及非硬链接检查继续生效；不跟随 symlink；子目录权限是明确作用域。
- 不运行 shell、脚本、Git hook、模型反序列化和外部网络请求，SQL 只包含硬编码的只读 SQLite 查询。
- 本地统计不是隐私保护技术：获得权限的内容可能进入 ChatGPT 对话。只给可信项目授权；避免授权有个人隐私或商务机密的数据。
- 项目里的危险可执行文件不在数据 profile 内，不能借此功能越过普通项目写回的敏感文件审核。

## 部署边界

新增 MCP 代码提交到 GitHub 开发分支 **不等于本机已升级**。原本正在运行的服务暂时不会自动出现这些 MCP 工具。需要在现有正常运行的 Ubuntu Bridge 后端上部署包含 v0.6 的代码并重新启动**一次**；后续数据 scope 变更无需重复重启。

此阶段继续保留运行稳定的旧版作为回退。正式验收应当在 AirGym 的**临时副本或无害数据**上依次测试：批准格式→列目录→读取大型 CSV→Rosbag DB3 主题元信息→局部文本读取→撤销权限→确认再次读取被拒绝。不要直接修改采集到的原始雷达包或飞行轨迹数据。
