# server-bootstrap

在 Debian/Ubuntu 服务器上一键安装常用开发环境：基础包、[ble.sh](https://github.com/akinomyoga/ble.sh)、[Oh My Bash](https://github.com/ohmybash/oh-my-bash)、SSH ed25519 密钥（若不存在）、以及 [uv](https://docs.astral.sh/uv/)。

## 要求

- 系统：`apt` 系发行版（如 Debian、Ubuntu）
- 当前用户具备 `sudo` 权限
- 网络可访问 GitHub 与 astral.sh

## 使用方式

在目标机器上执行：

```bash
curl -fsSL https://raw.githubusercontent.com/Qiuyc18/server-bootstrap/main/init.sh | bash
```

若你使用自己的 fork 或分支，把 URL 中的 `Qiuyc18/server-bootstrap` 与 `main` 改成你的仓库与分支名即可。

## 安装完成后

脚本会提示重新登录，或执行：

```bash
source ~/.bashrc
```

以使 ble.sh 与 Oh My Bash 生效。

## 本地运行

克隆仓库后也可直接执行：

```bash
bash init.sh
```
