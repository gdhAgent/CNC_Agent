"""
app.ingest —— 入库 / 解析层。

- alarm_parser.py  报警码表结构化解析 / 归一化 / 校验。
- loaders.py       PDF / Markdown 统一加载为 Page。
- chunker.py       手册 / SOP 分块策略。
- pipeline.py      解析→分块→入库 编排。

包级导入暂留空；业务代码直接 from app.ingest.<module> import ...。
"""
