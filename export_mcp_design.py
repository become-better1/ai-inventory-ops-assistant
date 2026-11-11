"""Export protocol schemas and a Typora-friendly design; only uses a temporary database."""
import asyncio
import json
from pathlib import Path
import tempfile
from mcp import Client
from mcp_server import create_server, ToolAdapter, DESCRIPTIONS

ROOT=Path(__file__).resolve().parent

async def main():
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)/'design.sqlite3'
        adapter=ToolAdapter(path)
        async with Client(create_server(path),raise_exceptions=True) as client:
            tools=(await client.list_tools()).tools
        document=dict(design_version='1.0',sdk='mcp==2.3.0',transport='stdio',
            tools=[t.model_dump(mode='json',by_alias=True,exclude_none=True) for t in tools])
        (ROOT/'mcp-tools.schema.json').write_text(json.dumps(document,ensure_ascii=False,indent=2),encoding='utf-8')
        text="""# MCP 工具接口设计 v1.0

**产品基线：** PRD v4.6，数据库设计 v1.0  
**日期：** 2026-11-10  
**本轮范围：** 23 个已有本地业务能力的 MCP 封装与协议验证  
**实现：** 官方 MCP Python SDK 2.3.0，Python 3.14.3，本地 stdio

## 一、调用链与边界

```text
本地 MCP 客户端
  → MCP 协议 / stdio
  → mcp_server.py（严格参数、错误结果、预览编号）
  → 已有 Assistant 业务接口
  → 用户自己的 SQLite 数据库
```

本轮不调用模型 API、不需要 API 密钥、不购买服务器、不监听 HTTP 端口。数据库路径由启动参数 --db 指定，工具不能修改数据库路径或执行任意 SQL。

本地协议测试通过不等于 ChatGPT 已完成连接。远程 HTTPS、访问认证、目标 ChatGPT 账户接入与自然语言实测属于后续工作，当前没有公网服务。最终远程发布仍必须满足 PRD 的认证与备份恢复要求。

工具描述与服务器 instructions 提供 AI 行为规则；自然语言意图识别、截图识别与用户真实确认，仍由未来宿主/AI 调用层执行，本服务不会读取聊天或验证自然语言中的用户身份。

## 二、请求和返回约定

所有工具使用具名 request 对象。例如录入购买：

```json
{
  "request": {
    "request_key": "purchase-unique-001",
    "name": "鼠标",
    "quantity": 2,
    "total_amount_minor": 40000,
    "currency": "CNY"
  }
}
```

数量、金额和版本使用严格整数，拒绝把 true 或字符串 "2" 转为整数。金额以最小货币单位传入，40000 表示 400 元人民币；NULL 表示未知，不等于零。未知字段拒绝。模型看到的完整输入输出定义见同目录 mcp-tools.schema.json，SDK 从 request 模型生成协议 Schema。

成功返回：

```json
{"ok": true, "data": {"purchase_id": "...", "item_id": "...", "operation_id": "...", "duplicate": false}}
```

业务拒绝返回：

```json
{"ok": false, "error": {"code": "INVALID_OR_STALE", "message": "..."}}
```

返回同时提供 MCP structuredContent 和兼容文本 JSON。所有工具声明为结构化输出，data 根据工具返回对象、列表或预览信息。

| 错误码 | 意义 | AI 下一步 |
|---|---|---|
| POSSIBLE_DUPLICATE | 疑似重复，带 candidates | 结合候选核实是否同一物品/订单，不静默重试新增 |
| ORDER_CONFLICT | 完整订单身份冲突，带 purchase_id | 更正既有记录或询问，不绕过去重 |
| INVALID_OR_STALE | 输入无效、版本变化、预览失效等 | 查询当前状态或必要时询问 |
| DATA_CONFLICT | 数据库约束冲突，未提交 | 重新查询核对 |
| INTERNAL_ERROR | 未预期故障 | 不宣称成功；检查服务日志和已有结果 |

参数 Schema 验证失败、未知工具名等由 SDK 通过 MCP isError 返回；业务拒绝由 ok=false 返回，协议本身可以成功完成。客户端必须同时检查两者，不可只看 isError。内部错误不向客户端返回堆栈、文件路径或数据库详情。

## 三、写入、版本与撤销

- 每次逻辑写入使用独立 request_key；同一请求重试沿用相同键与内容。
- 返回 operation_id 标识该操作；撤销必须明确使用对应 ID，不能猜测别的聊天的最近操作。
- 更改物品、房间、准则或退货事件前查询当前 version，再传 expected_version。
- 原操作撤销后审计版本仍增加；有后续变更或新增关联时保守地拒绝自动覆盖。
- 重复订单匹配没有新增业务快照，不把重复请求的 operation_id 当作新的购买来撤销。
- 不提供任意文件读取、备份路径修改、任意 Python 或 SQL 执行工具。
- 只读注解表示不写业务数据；预览会在进程内保存短期计划。

## 四、批量确认与服务端预览

三个预览接口返回 preview_id、preview 和 expires_in_seconds。执行接口只接受 preview_id，不接受模型拼出的 plan。

1. 先调用对应 preview 工具。
2. 展示受影响分组、数量、单位和目标变化。
3. 多组减少、批量移动、删除房间须取得用户确认。
4. 将原 preview_id 和 confirmed=true 交给对应执行工具。
5. 写事务内重查数据版本与范围，过期则重新预览和确认。

单分组明确数量修正或补录未知来源数量，不额外强制用户确认。needs_allocation=true 时，先明确减少哪些分组，再带 removals 重新预览；不能执行旧歧义计划。

预览保存在当前服务进程内，15 分钟有效，最多 256 个；超额淘汰最早的预览。服务重启后失效，同一进程只服务一位用户。已有确认预览仍可用于同键重试；过期或重启后不能直接重放，需先核实操作是否完成，再生成新计划，不能盲目新增。

confirmed 是调用端落实用户决定的约定，不是身份认证或授权凭证。未来远程接入需单独实施访问认证，宿主仍需落实真实用户确认。

## 五、工具清单与参数

以下与实际 request 模型同步生成。必填字段必须提供；可选字段可省略，具体默认值见 JSON Schema。

"""
        for name,(description,readonly) in DESCRIPTIONS.items():
            text+=f"### {name}\n\n**类型：** {'只读/预览' if readonly else '业务写入'}  \n{description}\n\n"
            model=adapter.models[name]
            if not model.model_fields:
                text+="参数：request 传空对象。\n\n"
                continue
            text+="| 参数 | 必填 | 默认值 |\n|---|---|---|\n"
            for field_name,field in model.model_fields.items():
                required=field.is_required()
                default="—" if required else json.dumps(field.default,ensure_ascii=False)
                text+=f"| {field_name} | {'是' if required else '否'} | {default} |\n"
            text+="\n"
        text+="""## 六、重要交互规则

- “想买”不写入，“买了”才调用 record_purchase；“已有”使用 record_owned。
- 普通照片先一次确认；明确信息的订单截图可按规则直接录入。
- 物品相似与用途相近不同；库存为空不能直接断言用户没有。
- 不因长期未提及就认定闲置。
- 单个库存分组可能含多件，move_item 移动整组，不是自动移动其中一件。
- 数量纠错按规范化精确名称与单位，跨型号/类别总数需先核实。
- 已退货同步抵扣，不询问到账；金额未知时说明统计不完整。
- 修改消费准则原文时不保留过时范围解释；临时例外不修改长期准则。
- 购买建议参考库存、启用准则和可用消费数据，但不替用户作最终决定。
- 无自动处置、支付、周期通知或注册登录工具。

## 七、本地启动与验收

安装依赖、启动、执行测试见 README 的 MCP 小节。mcp_server.py 运行后等待客户端协议输入，不提供普通聊天窗口，关闭客户端后进程退出。

验收包含：工具发现及 JSON Schema、只读标记、结构化输出、严格输入与未知字段拒绝、真实子进程 stdio 握手/发现/录入/查询、完整购买退货撤销、预览确认/过期/范围变化、消费准则和房间操作、数量纠错、金额更正以及重复订单错误映射。

这批工具复用已经实现的业务范围，不宣称 F01–F12 已全部开发。高级语义查询、图片识别、购买价值分析、原购买历史更正、部分分组移动、完整维护工具等仍待后续实现。

## 八、技术依据

- [官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [官方工具与类型模型说明](https://py.sdk.modelcontextprotocol.io/servers/tools/)
- [官方客户端测试说明](https://py.sdk.modelcontextprotocol.io/get-started/testing/)
- [官方运行方式](https://py.sdk.modelcontextprotocol.io/run/)

本轮以实际安装的 mcp==2.3.0 和本机协议测试为准，不把网页示例当作已通过的测试结果。
"""
        (ROOT/'MCP 工具接口设计 v1.0.md').write_text(text.replace('23 个',f'{len(tools)} 个'),encoding='utf-8')
    print('Exported MCP interface document and JSON schemas; personal database untouched.')

if __name__=='__main__':
    asyncio.run(main())

