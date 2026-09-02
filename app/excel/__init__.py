"""
app.excel —— Excel 导入导出业务层。

- templates.py  模板元数据与生成（get_columns / generate_template_bytes）。
- validate.py   xlsx 解析 + 行校验（validate / confirm 两阶段）。
- export.py     按条件导出 xlsx（列结构与模板一致，改完可再导入）。

业务代码直接 from app.excel.<module> import ...。
"""
