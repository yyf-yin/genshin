# 第三方代码声明

`src/genshin_assistant/bili/` 下的 B 站接口调用、WBI 签名、请求保护（节流、风控熔断、元信息缓存）、
字幕和置顶评论的获取方式、抽帧拼图，移植自 [dsh-bili-whale](https://github.com/Alterssss/dsh-bili-whale)
（Node.js 写的 DeepSeek Harness 插件，本仓库改写成 Python，只保留只读部分）。原项目许可如下：

```
MIT License

Copyright (c) 2026 ALTERSSSS

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

抽帧依赖本机安装的 ffmpeg（LGPL/GPL），本仓库不打包它。本项目与 B 站、DeepSeek 官方均无隶属关系。
