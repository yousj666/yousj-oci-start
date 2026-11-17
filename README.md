# Yousj OCI Start — 甲骨文自动抢机（自研版）

只用 OCI 官方 API 循环尝试创建实例，碰到「Out of host capacity」自动换可用域重试，
抢到为止。凭据只读本机 `~/.oci/config`，不经过任何第三方服务。

## 准备

1. Python 3.10+，装依赖：`pip install oci`（或用项目内 venv）
2. 在甲骨文控制台生成 API 密钥，把官方给的配置片段存到 `~/.oci/config`，私钥文件放好
3. 准备一对 SSH 密钥（登录开出来的机器用），公钥路径填进 job 配置
4. 复制 `job.example.json` 为 `job.json`，按需改：
   - `region`：区域，如 `ap-tokyo-1`（东京）/ `ap-osaka-1`（大阪）/ `ap-singapore-1`（新加坡）/ `ap-chuncheon-1`（春川）
   - `targets`：要抢的机型与数量。默认 = ARM A1.Flex 2核12G ×1 + AMD 小鸡 ×2（当前 Always Free 上限内）
   - `subnet_id` / `image_id` / `compartment_id` 留空 = 自动选（子网取区间内第一个，镜像取该机型最新 Oracle Linux）
   - `interval_seconds`：重试间隔（默认 60 秒，别太小）；`max_attempts`：0 = 不限次数

## 运行

```bash
# 先空跑验证配置（不真开机）
python3 yousj_oci_start.py --job job.json --dry-run

# 正式开抢
python3 yousj_oci_start.py --job job.json
```

抢到后想第一时间收到 Telegram 通知，运行前设置环境变量
`OCI_START_TG_TOKEN`（bot token）和 `OCI_START_TG_CHAT`（chat id）。

## 安全说明

- API 私钥 = 账号钥匙：只放自己机器上，不要发给任何人、不要用陌生人打包的同类工具
- 更稳妥的做法：在控制台单独建一个子用户给本工具用，权限只给开机所需，出事伤不到主号
- 配额/权限类错误（LimitExceeded、NotAuthenticated 等）会立即停下并报错，不会无意义死循环

## 测试

`python3 test_mock.py` — 模拟 API 的无容量重试、配额停机与错误分类，不连真实账号。

> 真机联调需要一个可用的甲骨文账号（API 凭据）；账号到位后先 `--dry-run` 再正式跑。
