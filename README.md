# Books of Time 岁月史书

Books of Time 是一个面向 Bilibili 二游社区公开视频与评论区的时间序列归档系统，用于在合规请求预算内记录、回放和分析游戏社区舆论事件的形成、扩散、转向与沉淀过程。

项目追踪文档：

- [ROADMAP](docs/ROADMAP.md)：长期阶段、里程碑和验收标准
- [TODO](docs/TODO.md)：当前任务清单和进度跟踪
- [DEPLOYMENT](docs/DEPLOYMENT.md)：Docker、Linux systemd、Windows 开发与数据库迁移
- [LOGIN](docs/LOGIN.md)：二维码登录、Cookie 自动刷新和凭据安全说明

## 项目约束

### 数据库迁移

Alembic revision 是部署契约，必须提交到 Git。修改 ORM 后使用 `uv run alembic revision --autogenerate` 生成 revision，人工审查 upgrade/downgrade，再运行 `uv run alembic upgrade head` 和 `uv run alembic check`。新库可使用 `uv run python main.py init-db`；现有无版本数据库的严格接管方法见 [DEPLOYMENT](docs/DEPLOYMENT.md)。

### 包管理器：uv

本项目强制使用 [uv](https://docs.astral.sh/uv/) 作为 Python 包管理器。

```bash
# 安装 uv（如果还没有）
winget install --id=astral-sh.uv  # Windows
curl -LsSf https://astral.sh/uv/install.sh | sh  # macOS / Linux

# 添加依赖
uv add <package>

# 移除依赖
uv remove <package>

# 安装全部依赖（含 dev）
uv sync --group dev

# 运行脚本
uv run python main.py
```

> ⚠️ **禁止**使用 `pip`、`pipenv`、`poetry` 等替代工具。`uv.lock` 是锁文件，必须提交到 git。

---

### 代码格式化：Ruff

使用 [Ruff](https://docs.astral.sh/ruff/) 同时做 lint 和 format：

```bash
# 手动运行
uv run ruff check .
uv run ruff format .

# 自动修复
uv run ruff check --fix .
```

配置见 `pyproject.toml` 中的 `[tool.ruff]` 段落。

---

### 提交信息规范：Conventional Commits

所有 git commit message 必须遵循 [Conventional Commits](https://www.conventionalcommits.org/) 格式：

```text
<类型>: <简短描述>

<可选详细说明>
```

允许的类型：

- `feat` — 新功能
- `fix` — 修复
- `docs` — 文档
- `style` — 样式（不影响代码含义的修改）
- `refactor` — 重构
- `perf` — 性能优化
- `test` — 测试
- `build` — 构建系统/依赖变更
- `ci` — CI 配置变更
- `chore` — 杂项
- `revert` — 回滚

---

### 行尾：LF

项目统一使用 **LF**（`\n`）行尾，禁止 CRLF（`\r\n`）。

- `.gitattributes` 已配置 `* text=auto eol=lf`
- pre-commit hook `mixed-line-ending` 会强制转换
- git 全局建议设置：`git config --global core.autocrlf input`

---

## 本地开发环境设置

```bash
# 1. 安装依赖（含 dev group）
uv sync --group dev

# 2. 激活虚拟环境（可选，但推荐）
.venv\Scripts\activate

# 3. 安装 pre-commit hooks（必须 —— 否则提交会被拦截）
uv run pre-commit install
uv run pre-commit install --hook-type commit-msg

# 4. （可选）手动运行 pre-commit 检查所有文件
uv run pre-commit run --all-files
```

> 如果遇到 pre-commit 环境问题，可以清除缓存后重试：
>
> ```bash
> Remove-Item "$env:USERPROFILE\.cache\pre-commit" -Recurse -Force
> uv run pre-commit run --all-files
> ```
