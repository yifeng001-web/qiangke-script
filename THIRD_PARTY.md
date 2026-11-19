# 第三方说明

本助手使用 [CMKH1337/Astrwechat](https://github.com/CMKH1337/Astrwechat) 的本机 HTTP/SSE 接口；接口适配参考下载仓库 `1.5.1/docs/HTTP-API.md` 和 `1.5.1/electron/services/httpService.ts`，仓库检查时提交为 `5cc51c1c740d470aaea7539b43b191908def6f60`。

Astrwechat 仓库附带 GNU AGPLv3 许可。本分发包不包含其源代码、程序、数据库、解密模块或图片资源；使用者自行安装并依其许可使用。这里的接入客户端独立编写，不依赖其 AstrBot 桥接程序，不配置自动回复。

Python 依赖由 pip 从软件包源安装，没有打包其二进制文件：Requests、NumPy、Pillow、OpenCV、uiautomation、comtypes。各依赖的许可及版权信息随对应安装包提供。

接口发生变化时，应先对照 Astrwechat 官方接口文档更新适配，不要将本机 AccessToken、数据库路径或聊天数据提交到公开仓库。
