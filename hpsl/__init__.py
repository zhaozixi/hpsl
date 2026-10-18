"""HPSL（项目制学习框架工具）后端包，Apache-2.0。

M0「多项目与数据底座」：每项目独立目录 + SQLite 库、迁移、配置；跨项目零共享。
依赖方向（规格 §3）：roles|engine|web → common,models；common/models 不反向依赖业务模块。
"""

__version__ = "0.0.0"
