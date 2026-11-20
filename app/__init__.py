"""容器配置文件路径诊断 API。

模块划分：
- archive:  安全解开外层 OCI Image Layout tar
- oci:      解析 index/manifest/config，定位并校验层 blob
- layers:   把各层 tar 合并成一棵带遮蔽来源的内存文件树
- resolver: 按容器视图解析绝对路径（含挂载点与符号链接环检测）
- models:   请求/响应模型
- main:     FastAPI 路由
"""

__all__ = ["__version__"]
__version__ = "1.0.0"
