# 湾区订餐助手代理服务

这是一个基于Google ADK框架开发的智能订餐助手，专为北美湾区（旧金山、伯克利、奥克兰、帕洛阿尔托等）的用户提供服务。通过这个代理服务，用户可以方便地查找餐厅、订购外卖或预订餐厅。

## 功能特点

- **餐厅搜索**：根据用户指定的菜系、位置和价格范围推荐湾区的优质餐厅
- **外卖订购**：帮助用户从选定餐厅订购外卖，填写并提交订单（自动设置默认送达时间）
- **餐厅预订**：协助用户在湾区热门餐厅预订餐位

## 支持的菜系

目前系统支持以下几种常见菜系的餐厅查询：
- 披萨 (Pizza)
- 中餐 (Chinese)
- 墨西哥菜 (Mexican)
- 印度菜 (Indian)
- 日本料理 (Japanese)

## 使用前准备

### 环境要求

- Python 3.9 或更高版本
- UV 包管理工具

### API密钥设置

在项目根目录创建 `.env` 文件并添加Google API密钥：

```bash
echo "GOOGLE_API_KEY=your_api_key_here" > .env
```

如果使用Vertex AI，则添加：

```bash
echo "GOOGLE_GENAI_USE_VERTEXAI=TRUE" >> .env
```

### 安装依赖

```bash
uv pip install -e .
```

## 运行代理服务

在项目目录执行以下命令启动服务：

```bash
uv run .
```

默认情况下，代理服务将在 `http://localhost:10002` 上运行。

要指定自定义主机和端口，可以使用：

```bash
uv run . --host 0.0.0.0 --port 10003
```

## 使用示例

### 1. 查找餐厅

用户可以询问关于特定菜系、位置或价格范围的餐厅推荐，例如：

- "我想找湾区的中餐馆"
- "旧金山有什么好的披萨店推荐吗？"
- "伯克利附近有什么价格适中的日本料理？"

### 2. 订购外卖

用户可以通过以下方式订购外卖：

- "我想点一份披萨外卖"
- "从Zachary's Chicago Pizza订餐"
- "我想订购中餐外卖送到家里"

系统会引导用户填写必要信息（餐厅名称、食物项目和送达地址），而其他信息则自动设置：
- 送达时间默认为当前时间30分钟后
- 特殊要求默认为"没有"

这样简化了订餐流程，用户只需提供最基本的信息。

### 3. 预订餐厅

用户可以预订餐厅：

- "我想预订餐厅"
- "今晚在Mister Jiu's预订4人的位子"
- "明天晚上7点帮我在Rintaro预约两个人"

## 使用A2A CLI客户端测试

使用A2A CLI客户端测试代理服务：

```bash
cd samples/python/hosts/cli
uv run . --agent http://localhost:10002
```

## 开发者信息

### 添加新餐厅

要添加新的餐厅到数据库，编辑`agent.py`文件中的`RESTAURANTS`字典，遵循以下格式：

```python
"cuisine_category": [
    {"name": "Restaurant Name", "location": "City", "cuisine": "Specific Type", "price_range": "$$", "rating": 4.5},
    # 更多餐厅...
]
```

### 添加新功能

如果需要添加新功能，可以：

1. 在`agent.py`中定义新的工具函数
2. 在`_build_agent()`方法中将新函数添加到tools列表
3. 更新代理指令以处理新功能

## 技术架构

代理服务基于Google的Agent Development Kit (ADK)构建，使用A2A协议进行通信。主要组件包括：

- **FoodOrderingAgent**：核心代理类，处理用户请求
- **AgentTaskManager**：管理代理任务和通信
- **A2AServer**：提供A2A协议接口

## 故障排除

- **API密钥错误**：确保`.env`文件中设置了正确的`GOOGLE_API_KEY`
- **端口冲突**：如果10002端口已被占用，使用`--port`参数指定其他端口
- **模型访问错误**：确保您有权访问指定的LLM模型（如`gemini-2.0-flash-001`）

## 贡献指南

欢迎贡献代码或提交问题！请遵循以下步骤：

1. Fork项目仓库
2. 创建功能分支
3. 提交更改
4. 创建Pull Request

## 许可证

本项目基于MIT许可证发布。
