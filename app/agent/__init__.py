"""
app.agent —— 受限工具路由 Agent：
tools（工具 schema + 实现）、router（显式状态机主入口）、prompts（提示词）、
output（结构化输出解析 + 拒答判定 + 渲染）。

定位红线：只做「检索 + 辅助分析」，绝不做机台控制 / 指令下发。
"""
