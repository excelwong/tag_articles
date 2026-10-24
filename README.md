# tag_articles — 文章标签分析工具

遍历指定目录下所有 txt 文件，调用本地大模型 API（OpenAI 兼容接口）
分析文章情节并按预设标签体系打标签，结果写入 `article_tags_llm.json`
（可配合 `gen_browser` 渲染成网页浏览）。

## 用法

```bash
python tag_articles.py
```

或双击 `tag_articles.bat`。

## 功能

- **断点续传**：已分析过的文件（按相对路径匹配）不重复请求，中断后重跑自动跳过
- **增量落盘**：每分析完一个文件立即写盘，中途中断不丢进度
- **分段处理**：超过长度阈值的文件自动切段独立分析，结果取并集
- 实时输出序号、文件名、分析摘要与标签

## 配置

脚本顶部配置区（相对路径基于脚本所在目录）：

| 项 | 说明 |
|---|---|
| `API_URL` | 本地 LLM 的 chat/completions 地址，默认 `http://127.0.0.1:1234/v1/chat/completions` |
| `API_KEY` | API 密钥（本地服务填模型名占位即可） |
| `API_TIMEOUT` | 单次请求超时（秒） |
