# 部署手册

饶信谱脉后端的云服务器部署。所有脚本在 `_deploy/` 下，都用本机 Python 跑。

## 零、跑之前：用哪个 Python

> ⚠️ **本手册里的 `python` 一律指项目的虚拟环境**，不是系统 PATH 里的那个：
>
> ```bash
> backend\.venv\Scripts\python.exe _deploy\deploy.py --config
> ```
>
> 为什么单列一条：这台机器上有**三套解释器**——WorkBuddy 托管的 3.13.12、
> `~/.workbuddy/binaries/python/envs/default` 的 3.13.14、系统 PATH 里的 3.14。
> `paramiko` 只装在 venv 里，裸 `python` 撞上另外两套就会报
> `ModuleNotFoundError: No module named 'paramiko'`（2026-10-17 发生过一次）。
> 依赖清单在 `_deploy/requirements.txt`，没装过就先 `pip install -r` 一遍。

---

## 一、服务器

| 项 | 值 |
|---|---|
| 公网 IP | `150.158.176.220` |
| 后端域名 | `api.wangqiming.top`（DNS 在**阿里云**，A 记录指向上面这个 IP） |
| 备案 | 在**腾讯云**备案，接入商即腾讯云（跨厂商的话要先做接入备案） |
| 系统 | Ubuntu 24.04 LTS |
| 规格 | 4 核 / 3.6G 内存 / 40G 盘（含 1.9G swap） |
| 登录 | `ubuntu`（sudo 免密），密钥 `~/.ssh/id_ed25519_raoxin` |
| 位置 | 腾讯云轻量应用服务器 |

> ⚠️ **域名注册商 / DNS 服务商 / 备案接入商 / 服务器厂商是四件事**，别混。
> 这里：域名在阿里云注册、DNS 也在阿里云、备案做在腾讯云、服务器是腾讯云。
> 只要**备案接入商 = 服务器厂商**就不需要额外动作；不等就得做接入备案，
> 否则云厂商会在网络层拦掉 80/443。

**它原本不是一台干净机器**——是腾讯云的「LNMP 应用镜像」，预装了 Apache +
MariaDB + PHP-FPM，装在 `/usr/local/lighthouse/softwares/` 下，由 systemd 托管。
我们一个都不用，但处理它们时踩了坑（见第五节）。

---

## 二、架构

```
        公网 api.wangqiming.top   ← 阿里云 DNS 的 A 记录 → 150.158.176.220
                    │
              :443 ─┴─ nginx           ← 对外唯一入口
                    │   sites-available/raoxin
                    │   （:80 只做 301 跳转 + ACME 验证）
                    ▼
              127.0.0.1:8000
              uvicorn (raoxin-api.service)   ← 不对外，只监听本地
                    │
                    ▼
              127.0.0.1:3306
              MySQL 8.0.46                    ← 不对外
```

三层都在同一台机器上，通过本地回环连接。**对外只开 80 和 443**（22 给 SSH）。
3306 在腾讯云防火墙上本来就不通，这一点对安全很有利，不要为了"方便调试"去放开它。

> IP 直连（`http://150.158.176.220/v1`）仍然保留着：nginx 里有一个
> `listen 80 default_server` 的 server 块专门接它，方便 PC 上不开域名直接联调。
> 小程序端 `utils/env.js` 的 `cloudIp` 就是这个退路。

---

## 三、目录布局

```
/srv/raoxin-genealogy/backend/     代码 + 虚拟环境
├── .env                           生产配置（600，含数据库密码）
├── .venv/                         依赖
├── app/                           应用
├── scripts/                       建表/灌数据/自检脚本
└── sql/schema.sql                 表结构

/etc/systemd/system/raoxin-api.service    服务定义
/etc/nginx/sites-available/raoxin         站点配置
```

---

## 四、日常操作

### 改了后端代码要上线

```bash
python _deploy/deploy.py --code
```

这一步会：上传代码 → 重启服务 → 打公网地址跑契约巡检。

**它不会碰数据库**，随时可以跑。

### 改了表结构（加表 / 加列）

```bash
backend\.venv\Scripts\python.exe _deploy\migrate.py backend/sql/migrate_2026-10-17_user_family.sql
```

改表**刻意不并进 `deploy.py`**。`deploy.py` 的注释里写着「部署不会碰数据库」，
那是条有意留的边界：配置或代码写错时重跑部署是安全的，一旦它顺手改表结构，
「重跑一次」就变成有风险的动作了。

`migrate.py` 按这个顺序做，任何一步失败就停住：

1. 确认远端有 mysql 客户端
2. **先 `mysqldump` 一份整库**到 `<部署目录>/backups/<库名>-<时间戳>.sql.gz`，
   备份失败直接退出——没有退路就不改表
3. 上传 SQL 到 `/tmp`，用经 SFTP 传的临时 `my.cnf`（600）执行，密码不进命令行
4. SQL 里的 `SELECT` 原样打印，迁移脚本的自检查询就靠这个看结果
5. 删掉临时凭据和 SQL

所以增量脚本要**同时写建表语句和自检查询**，参考
`backend/sql/migrate_2026-10-17_user_family.sql` 的第 3 节：
它查的是「有族的账号是否都有对应成员行，且角色一致」，期望返回 0 行。

`backend/sql/schema.sql` 是 DROP + CREATE 的全量脚本，**不要拿去跑线上**。

### 改了 `.env` / nginx 配置 / systemd unit

```bash
python _deploy/deploy.py --config
```

配置的真实来源是 `_deploy/deploy.env`，改它然后重新部署，不要直接去服务器
上手改 `/srv/.../.env`——下次部署会覆盖掉。

### 切到真实微信登录（关掉 `WX_MOCK_LOGIN`）

**顺序不能颠倒**，否则中间会有一段时间谁都登录不了：

1. 先改小程序端（`utils/env.js` 的 `LOGIN_MODE` 按 **连哪个后端** 分流
   + `utils/session.js` 让旧 token 随环境作废），重新上传、选为体验版
2. 验密钥：

   ```bash
   backend\.venv\Scripts\python.exe _deploy\check_wx_secret.py
   ```

   拿一个假 code 调 `jscode2session`：**40029 = 密钥有效**（微信过了密钥这关才抱怨 code），
   **40125 = 密钥不对**。这样不用真登录就能验，避免填错一个字符就全员登录失败。
3. 最后把 `deploy.env` 的 `WX_MOCK_LOGIN` 改成 `false`，跑 `deploy.py --config`

> 为什么不能先关后端：一旦 `WX_MOCK_LOGIN=false`，后端不再认 `dev:` 前缀，
> 而体验版还是旧包（发的正是 `code: 'dev:mock_new_member'`），微信会回
> `40029 invalid code` → 所有人登录失败，认领和写入全废。

### 启用域名 + HTTPS（首次上线，或换域名时）

```bash
python _deploy/deploy.py --site
```

> **当前状态（2026-10-15）：已启用。** 再跑这条命令是**幂等**的——证书已存在
> 就会跳过签发（Let's Encrypt 有频率限制，别反复签）。验收结果：契约巡检
> 19/19、前端预检 50/50、证书链用系统默认 CA 校验通过（TLSv1.3 / ECDSA）。

**前置三件事**（脚本会检查，但早点做掉省一轮）：

1. 阿里云 DNS 加一条 A 记录：主机记录 `api` → `150.158.176.220`
2. 腾讯云轻量服务器的**防火墙放行 443**（80 之前就开着）
3. 备案的**接入商是腾讯云**。如果备案是在别家做的，得先在腾讯云做
   「新增接入备案」——否则腾讯云会在网络层拦掉，返回一个备案提示页

> 还有一件**不是前置、但会让你吓一跳**的事：**本机（开发机）的 DNS 可能还
> 缓存着 NXDOMAIN**。A 记录刚加上的那几十分钟，典型症状是"公共 DNS 查得到、
> 就这台机器解析不出来"，早年 curl 会直接报 `Could not resolve host`。
> 所以 `--site` 的第 2 / 第 5 步做了兜底：本机解析不出就按
> `curl --resolve` 的方式直连 IP 探测（**域名照旧参与 SNI 与证书校验**，
> 结论等价），`--verify` 也会退回 IP 巡检并打印说明。
> 它只影响**本机这一侧**的探测——Let's Encrypt 用自己的公网解析，
> 完全不受影响，所以不要因为本机解析不出来就以为证书签不了。

这条命令按顺序做五件事，**每一步过了才走下一步**：

| # | 做什么 | 为什么 |
|---|---|---|
| 1 | 铺 `SITE_TLS=off` 的 nginx 配置 | 只有 80 + ACME 验证目录。证书还没签出来，写 443 会让 nginx 起不来 |
| 2 | **从本机**打 `http://域名/health` | 这是 DNS / 备案 / 防火墙三件事的试金石，失败会直接告诉你去查哪一条 |
| 3 | 装 certbot | |
| 4 | `certbot certonly --webroot` 签证书 | 幂等：已有证书就跳过（Let's Encrypt 有频率限制，别反复签） |
| 5 | 翻 `SITE_TLS=on`、铺 443 配置、reload、**从本机**打 `https://域名/health` | |

第 5 步会自动把 `deploy.env` 里的 `SITE_TLS` 改成 `on`。**别手动改回 `off`**——
下次 `--config` 会按它渲染，写回 `off` 就把线上 443 覆盖没了。

#### 为什么 nginx 配置要分两版

因为证书是鸡生蛋：**开 443 需要证书，拿证书需要域名能通过 80 访问**
（Let's Encrypt 要从公网取一个随机文件来核对域名所有权）。

`SITE_TLS` 就是这个阶段的开关，两版差别只在「有没有 443 块、80 是否跳转」。
生成逻辑在 `gen_configs.py::render_nginx()`，三个 server 块各管一件事：
IP 直连 / 域名的 80 / 域名的 443。

#### 续期

apt 装的 certbot 自带一个 systemd timer，每天跑两次 `certbot renew`，
续完自动 reload nginx——这是靠签证书时的 `--deploy-hook 'systemctl reload nginx'`
实现的，**不能省**：不加的话证书悄悄换了、nginx 还拿旧的，90 天后才发现。

```bash
python _deploy/remote.py 'sudo systemctl list-timers certbot.timer' \
                         'sudo certbot certificates'
```

### 只看服务现在健不健康

```bash
python _deploy/deploy.py --verify
```

只读，不改任何东西。打公网地址跑 19 项契约断言，包括**路由表比对**
（运行中服务的路由 vs 本地源码的路由），能抓出「服务器在跑旧代码」。

### 看日志

```bash
# 应用日志
python _deploy/remote.py 'sudo journalctl -u raoxin-api -n 50 --no-pager'

# 实时跟
python _deploy/remote.py 'sudo journalctl -u raoxin-api -f'

# nginx 访问日志
python _deploy/remote.py 'sudo tail -50 /var/log/nginx/raoxin.access.log'
```

### 重启 / 停止

```bash
python _deploy/remote.py 'sudo systemctl restart raoxin-api'
python _deploy/remote.py 'sudo systemctl stop raoxin-api'
```

### 服务器上直接执行命令

```bash
python _deploy/remote.py 'uptime' 'df -h /'
python _deploy/remote.py 'sudo mysql raoxin_genealogy -e "SELECT COUNT(*) FROM person;"'
```

`remote.py` 支持两种输入：

| 写法 | 含义 |
|---|---|
| `'命令'` | 直接在服务器上执行 |
| `'命令 <<<FILE 本地文件'` | 把本地文件内容喂给这条命令的 stdin（导 SQL 用） |

⚠️ `<<<FILE` **必须放在命令末尾**，它后面不能再接别的命令——后面的内容会被当成文件路径。

---

## 五、首次部署（换机器时照这个来）

```bash
# 0. 装公钥（只需一次，之后免密）
export DEPLOY_PASS='服务器密码'
python _deploy/setup_key.py

# 1. 装 MySQL 8 / nginx / Python 工具链
python _deploy/remote.py \
  'sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq' \
  'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y mysql-server nginx python3-venv python3-pip'

# 2. 建库建账号（幂等）
python _deploy/gen_configs.py
python _deploy/remote.py 'sudo mysql <<<FILE _deploy\.gen\init_db.sql'

# 3. 传代码 + 建虚拟环境 + 装依赖
python _deploy/upload.py
python _deploy/remote.py \
  'cd /srv/raoxin-genealogy/backend && python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt -i https://mirrors.cloud.tencent.com/pypi/simple'

# 4. 导表结构
python _deploy/remote.py 'sudo mysql --default-character-set=utf8mb4 raoxin_genealogy <<<FILE backend\sql\schema.sql'

# 5. 配置 + 启动
python _deploy/deploy.py --config
python _deploy/deploy.py --verify

# 6.（可选）灌演示数据
python _deploy/remote.py 'cd /srv/raoxin-genealogy/backend && .venv/bin/python scripts/seed_demo.py'
```

---

## 六、这台机器为什么这么配（踩坑记录）

### ① 没复用镜像自带的 MariaDB，而是另装了 MySQL 8

镜像里的 MariaDB 版本和 `schema.sql` 不兼容：建表脚本用的是
`utf8mb4_0900_ai_ci` 排序规则，**MariaDB 不支持它**。更根本的原因是本地开发
用的是 MySQL 8.0.46——两边都用 MySQL 8 才能保证「本地能跑、线上也能跑」，
不用维护两份建表脚本。

实际用下来端到端一致：本地 `utf8mb4_0900_ai_ci`，云端也是。

### ② 装 MySQL 时装挂过一次，根因是镜像留下的空壳数据目录

`apt install mysql-server` 报错：

```
[ERROR] [InnoDB] The innodb_system data file 'ibdata1' was not found
[ERROR] The designated data directory /var/lib/mysql/ is unusable
```

`/var/lib/mysql` 只有 8KB，里面有 `debian-5.7.flag`、`auto.cnf`、
`mysql-bin.index` —— 是**镜像预装的 MySQL 5.7 残留**。MySQL 8 的
`--initialize` 要求数据目录为空，看到有文件就直接拒绝，而且**不报错**（初始化
静默返回，文件数一个没多）。

处理：把目录挪走（备份，没删）、建空目录、手动 `mysqld --initialize-insecure`。

### ③ `/etc/my.cnf` 是 MariaDB 写的，污染了所有 MySQL 工具

MySQL 客户端连不上，报错指向 MariaDB 的 socket：

```
Can't connect to local MySQL server through socket
'/usr/local/lighthouse/softwares/mariadb/data/mariadb.sock'
```

`/etc/my.cnf` 里写死了 MariaDB 的 socket 和 `innodb_data_home_dir`。而这个文件
是 MySQL 读配置时**优先级最高**的位置之一——它影响的不只是客户端。
已归档为 `/etc/my.cnf.mariadb.bak`。

**判断依据**：`/etc/mysql/` 下才是 Ubuntu 的 MySQL 配置；`/etc/my.cnf` 是
MariaDB/源码安装喜欢放的地方。两者同时存在就是隐患。

### ④ `pkill -f mariadbd` 把自己也杀了

执行 `sudo pkill -f mariadbd` 时，**执行它的那个 shell 命令行里就含 "mariadbd"
这个字符串**，于是 pkill 匹配到了自己，SSH 通道直接断掉（返回码 -1）。
目标虽然达成了，但报错很吓人。

要按进程名精确匹配就用 `pgrep -x mariadbd` 拿 PID 再 kill。

### ⑤ 后端用单 worker，是有意的

`raoxin-api.service` 里写死 `--workers 1`。原因：**答题自证的限速桶是进程内的
dict**，多 worker 会各记一份，攻击者轮询就能绕过限速。

将来要横向扩展，得先把限速挪到 Redis。在那之前，单 worker 是安全约束，不是省事。

顺带一提，uvicorn 只监听 `127.0.0.1:8000`，对外一律走 nginx。这样加 HTTPS
只改 nginx，应用完全不用动。

### ⑥ 自检脚本必须绕过本机代理

本机环境变量里有 `http_proxy=http://127.0.0.1:61106`（某个代理软件），
httpx 默认会读它。于是 `check_live.py` 的成败**取决于代理进程当时活着没有**：
代理活着请求就绕道走（看到的是代理的行为），代理一关就报「连接被积极拒绝」，
看起来像服务器挂了。

已在 `check_live.py` 里显式 `trust_env=False`。`curl` 要直连得加 `--noproxy '*'`。

---

## 七、待办

### ⚠️ 上线前必须处理

- [x] **关掉 `WX_MOCK_LOGIN`（2026-10-17 完成）**。`deploy.env` 填了 `WX_SECRET`
      （32 位，用 `check_wx_secret.py` 拿假 code 验过：微信回 `40029`，
      说明密钥这关过了），`WX_MOCK_LOGIN=false` 已部署。
      线上 `dev:` 前缀现在被拒（契约巡检【1.5】断言：`实际关`），
      意味着 **只有真实 `wx.login()` 能登录**。
      副作用要知道：库里 `mock_*` 那几个演示账号从此登不进去了，
      端到端测试只能在本机（`APP_ENV=dev` + `WX_MOCK_LOGIN=true`）跑。
      端上分流逻辑在 `miniprogram/utils/env.js`：`LOGIN_MODE = DEV_MODE && BACKEND === 'local' ? 'dev' : 'wx'`，
      **按连哪个后端分流**，不是按 `envVersion`——真机调试也是 `develop`，
      按版本号分会让「工具通、体验版不通」。
- [x] **域名 + HTTPS（2026-10-15 完成）**。`api.wangqiming.top` 已启用：
      Let's Encrypt 证书（ECDSA，TLSv1.3，90 天有效期到 2027-01-13），
      `certbot.timer` 自动续期（`renew --dry-run` 已验过），80 已 301 跳 443。
      验收：契约巡检 19/19、前端预检 50/50、证书链从系统默认 CA 校验通过。
      **剩最后一步人为操作**：微信小程序后台 → 开发管理 → 开发设置 →
      服务器域名，把 `https://api.wangqiming.top` 配进 **request 合法域名**
      （不配的话真机上仍报 `request:fail url not in domain list`）。
      ⚠️ 没填 `CERT_EMAIL`，所以签证书时走的是
      `--register-unsafely-without-email`——**证书照常自动续期，只是续期失败
      时收不到邮件通知**。想收通知：填上 `CERT_EMAIL` 重跑 `--site`（幂等）。

### 其他

- [ ] 数据库目前只有演示数据（19 人）。真实家谱数据导入方案未定：
      继续用 `seed_demo.py` 那样的脚本，还是走 Excel/CSV 导入工具。
- [ ] 没有自动备份。MySQL 数据在 `/var/lib/mysql`，整机快照靠腾讯云的策略。
      真实数据进来前应该加一个每日 `mysqldump` 定时任务。
- [ ] 监控告警没有。服务器挂了不会有人知道，除非去打开小程序发现报错。
- [ ] `_deploy/deploy.env` 是纯明文凭据文件。目前只在本机，**不要提交、
      不要放进任何同步盘**。

---

## 八、凭据在哪

全部在 `_deploy/deploy.env`（已在 `.gitignore` 中排除）：

| 键 | 用途 |
|---|---|
| `DB_PASSWORD` | MySQL 应用账号 `raoxin` 的密码 |
| `SECRET_KEY` | JWT 签名密钥，**与开发环境不同** |

服务器上的 `/srv/raoxin-genealogy/backend/.env` 由 `gen_configs.py` 渲染生成，
权限 600。**改配置请改本地的 `deploy.env`**，然后 `python _deploy/deploy.py --config`。
