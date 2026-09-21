# 闻录独立知识库生成工具

本工具位于 `E:\wenlu-kb-builder`。它与闻录代码、运行环境、打包流程相互独立，不会把 demo 或生成结果加入闻录安装包。

选择资料 → 本地解析和切块 → 本地向量化 → 输出 `.wlkb` → 搜索验证。

新版统一使用 `.wlkb` 后缀。原 `.wenlukb` 包的数据结构不变，可直接改名为 `.wlkb`，在新版闻录中移除旧路径并重新添加；不需要重新向量化。

## 开始使用

当前机器已经准备好独立环境和模型，双击 **start.cmd** 打开窗口。

1. 首次使用其他电脑时，安装 Python 3.12，再运行 `setup.cmd`。该步骤安装锁定版本的依赖，并从 Hugging Face 下载约 24 MB 模型。
2. 在窗口中填写客户 ID、名称，选择一个文件或资料文件夹，指定输出文件。
3. 点击「生成知识包」，等待日志显示完成。
4. 输入问题，点击「检索验证」，查看实际匹配的原文和来源。这里不调用大语言模型，不生成问答答案。
5. 使用「填入虚构样例」可直接选择附带的林舟 demo。只索引 `examples/linzhou/source`，验收题位于独立的 `evaluation` 目录。

模型已就绪后，构建和查询不会访问网络；资料不会上传。默认 CPU 推理，限制为 2 个计算线程，可在编码器初始化参数中调整。

## 命令行与自动化

在 PowerShell 中先执行 `Set-Location E:\wenlu-kb-builder`。

```powershell
# 模型只需准备一次；模型损坏时可重新执行。
.\.venv\Scripts\python.exe -m kbtool prepare-model

# 构建 demo。新客户应使用自己的客户 ID 和输出路径。
.\.venv\Scripts\python.exe -m kbtool build --source examples\linzhou\source --output output\linzhou-demo.wlkb --customer-id demo-linzhou --name linzhou-demo

# 混合检索；也可使用 --mode semantic 或 --mode keyword。
.\.venv\Scripts\python.exe -m kbtool search --package output\linzhou-demo.wlkb --customer-id demo-linzhou --query "数据库已经更新，但提醒没有发出去怎么办？"

# 不加载模型即可读取包内清单。
.\.venv\Scripts\python.exe -m kbtool inspect --package output\linzhou-demo.wlkb

# 实际模型回归测试和 demo 检索评估。
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe evaluate_demo.py
```

命令行结果为 stdout JSON，进度和错误写入 stderr，失败返回非零退出码。门户任务进程可直接调用；当前不包含门户、支付、授权或任务队列服务。使用自定义模型目录时将 `--model-dir 路径` 放在子命令之前；该目录必须包含固定模型文件，并不支持任意不同模型混用。

## 模型和切块

- 基础模型：BAAI/bge-small-zh-v1.5，512 维；使用 Xenova 发布的 ONNX 动态量化文件。
- 固定仓库版本：`75c43b069aac4d136ba6bc1122f995fedcfd2781`，每个下载文件都有固定 SHA256。
- CLS pooling、L2 归一化。查询添加模型的检索指令，文档不添加该查询指令。
- 默认正文每块最多 320 token，长段落切分重叠 48 token。同章节、同页的相邻短段落合并，章节标题参与向量化；所有输入检查模型 512 token 上限，不静默截断。
- 中文双字词元及英文标识符建立 SQLite FTS5 索引；向量与关键词分别召回后用 RRF 合并。关键词策略属于初版基线，不等同于语言学分词。

模型资料：[BAAI 模型卡](https://huggingface.co/BAAI/bge-small-zh-v1.5)、[ONNX 转换模型](https://huggingface.co/Xenova/bge-small-zh-v1.5)。模型由原发布者按 MIT 许可提供；分发模型时应遵循对应许可。

## 支持范围与限制

- TXT / Markdown / RST：UTF-8、UTF-16 BOM 或 GB18030，保留来源行范围。
- DOCX：提取正文、表格内文字和常见标题样式；行号为提取后的正文行号，不是 Word 页码。浮动文本框、批注、页眉页脚不作为完整文档支持范围。
- PDF：提取文本，保留页码与页内提取文本行范围。没有 OCR；出现无可提取文字的页面会拒绝发布，请先确认空白页或完成 OCR。
- 每文件最多 25 MB、200 万提取字符，PDF 最多 200 页；每包最多 1000 个文件、50000 个片段。实际速度和内存取决于资料规模。
- 文件夹默认跳过隐藏目录、evaluation、tests、models、output、build、dist、依赖目录和符号链接。其他不支持文件记录到报告。显式选择的单个文件仍需由操作者确认属于客户资料。
- 任何支持格式的文件解析失败，整次构建不发布新包；不会以部分成功冒充完整交付。
- 不生成结构化履历或补全经历；当前阶段保留用户提供的原文。档案抽取及人工审核属于后续模块。
- 检索得分不是事实置信度。系统尚未实现无答案判定、多轮指代改写、重排模型及自动答案生成。

## 产物和版本

`.wlkb` 是单个 SQLite 数据文件，不是加密归档。包含：

| 表 | 内容 |
| --- | --- |
| metadata | JSON manifest：客户 ID、包 ID、版本、来源文件哈希、完整模型配置、统计和限制 |
| chunks | 片段 ID、相对来源、章节、页码、行范围、原文、内容哈希、512 维 little-endian float32 向量 |
| keywords | FTS5 词元索引，rowid 与 chunks 对应 |

同目录 `.report.json` 是可读构建报告，包含包 SHA256。包内 manifest 是权威元数据；外部报告写入失败不撤销已经完成的包。

相同资料和配置再次构建，不修改知识包；资料变化时复用当前客户旧包中相同文本的向量，并递增版本。删除的资料不会进入新包。模型配置不同则重新编码；输出属于另一客户时拒绝覆盖。

先写临时 SQLite 并检查，再原子替换目标。取消或解析失败时保留原包；同一输出用 `.wlkb.lock` 避免并发构建。进程被强制终止后，如果留下锁文件，先确认没有任务运行，再手工移除对应锁文件。

知识包包含客户原文，没有加密、发行签名、激活逻辑或 API Key。SHA256 用于完整性检查，不能代替可信发行签名。

**闻录已增加本格式的加载适配。** 在更新后的闻录中，通过问答区「+ → 知识包」选择生成的 `.wlkb`。DeepSeek 和兼容 API 均可使用本地检索结果；同一时间最多启用 4 个同一客户的知识包。闻录内置相同编码配置的通用模型，不依赖本工具目录，也不打包客户资料。此前生成包内的旧版 limitations 提示可能仍写着“尚需实现加载适配”，不影响格式识别。

## 目录

```text
kbtool/                 构建、模型、检索、CLI 与窗口代码
tests/                  使用真实模型的回归测试
examples/linzhou/source/ 虚构简历与项目资料
examples/linzhou/evaluation/  独立验收题，不入库
models/                 本机模型文件
output/                 知识包、构建报告、检索评估报告
start.cmd               打开窗口
setup.cmd               新环境安装依赖并准备模型
build-demo.cmd          构建虚构样例
```
