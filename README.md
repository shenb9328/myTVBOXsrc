# 🎬 mytvboxsrc - 家庭私有影视聚合中枢系统

[![Python](https://img.shields.io/badge/Python-3.9+-3776AB?logo=python&logoColor=white)](https://python.org)
[![Platform](https://img.shields.io/badge/Platform-NAS%20%7C%20Linux-blue?logo=linux&logoColor=white)](https://github.com)
[![TVBox](https://img.shields.io/badge/Client-TVBox%20%7C%20影视仓-orange)](https://github.com)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

基于轻量级 Python + SQLite 架构打造的**家庭局域网 TVBox / 影视仓全网超级聚合中枢**。在 NAS 或局域网 Linux 设备上部署，自动巡检、测速、聚合全网各大主流点播资源站，实现**单片多线路自动容灾**、**多维悬浮交叉筛选（方案 B）**与**局域网毫秒级秒开**。

---

## 🌟 核心特性

1. **同名影片多线路自动聚合（智能容灾）**
   - 跨采集站自动完成标题清洗与同义归一化；
   - 一部电影/电视剧卡片内聚合 3~8 条独立播放线路（如 `[5线]`、`[8线]`）；
   - 单源卡顿或失效时，TVBox 可一键秒切至备用线路无缝续播。

2. **多维交叉筛选体系（专业级方案 B）**
   - 完美兼容 TVBox 原生筛选矩阵（遥控器按【上键】即时呼出）；
   - 支持**类型**（古装/都市/武侠/悬疑/科幻/喜剧等） × **地区**（国产/欧美/韩剧/日剧/港剧等） × **年份** 多维自由交叉；
   - 结合中国影视题材同义词语义扩展，海量片目满屏展示，彻底告别“没找到数据”。

3. **本地轻量 SQLite 镜像驱动（极致秒开）**
   - 单表+高命中复合索引，所有分类、搜索与筛选请求响应时间 **< 5ms**；
   - 电视端选台、滑动海报墙与翻页流畅顺滑无卡顿。

4. **全自动质量巡检与增量同步**
   - 内置并发健康检查与 m3u8 切片连通性测试，自动淘汰失效源与慢速源；
   - 每日凌晨 05:00 自动拉取全网最新上映大片与热播剧集，片库持久常新。

---

## 📸 电视实机效果

| 电视剧 · 古装大剧分类 (多线路聚合) | 电影 · 喜剧大片分类 (海报秒开) |
| :---: | :---: |
| ![电视剧古装展示](docs/images/tvbox_series_filter.png) | ![电影喜剧展示](docs/images/tvbox_movie_routes.png) |

---

## 📂 项目结构

```text
mytvboxsrc/
├── server.py              # 核心 Web 服务与超级私有源接口 (/api/vod, /vod.json)
├── db.py                  # 本地轻量 SQLite 镜像数据库与多线路聚合中枢
├── seed_categories.py     # 全分类深度填充引擎 (快速构建 8,000+ 部精品全维片库)
├── sync.py                # 全网采集站多线路定时同步引擎 (每日增量更新)
├── checker.py             # 采集源可用性与延迟巡检引擎 (API + m3u8 双重测速)
├── sources_pool.json      # 全网候选采集源备选池
├── valid_sources.json     # 巡检优选后的高可用主力源列表
├── vod.json / tvbox.json  # TVBox / 影视仓标准配置订阅文件
├── push_to_box.sh         # 一键 ADB 推送配置到机顶盒
├── refresh.sh             # 手动触发源巡检与配置刷新
├── status.sh              # 快速查看服务与片库状态
├── systemd/               # 生产级 systemd 守护进程与定时器配置
│   ├── tvbox-vod.service  # 主服务守护进程
│   ├── tvbox-sync.service # 定时同步服务
│   └── tvbox-sync.timer   # 每日凌晨 05:00 定时器
└── docs/images/           # 电视真机截屏与说明图片
```

---

## 🚀 快速开始

### 1. 环境准备

系统要求：Linux / NAS / 树莓派，已安装 Python 3.9+。

```bash
# 克隆仓库
git clone git@github.com:shenb9328/mytvboxsrc.git
cd mytvboxsrc
```

### 2. 初始化高可用源池并深度填充片库

```bash
# 1. 运行巡检程序，测试并筛选可用源
python3 checker.py

# 2. 运行全分类深度填充引擎（自动爬取各大主力源核心分类，构建 8,000+ 部本地库）
python3 seed_categories.py
```

### 3. 启动本地影视聚合服务

```bash
# 直接启动
python3 server.py

# 或后台静默启动
nohup python3 server.py > server.log 2>&1 &
```

服务默认监听 `0.0.0.0:5888`，浏览器访问 `http://<NAS_IP>:5888/` 可查看可视化运行控制面板。

---

## 📺 客户端配置 (TVBox / 影视仓)

### 方式一：直接在 TVBox 设置中输入订阅链接

打开 TVBox / 影视仓 -> **设置** -> **配置地址**，输入：
```text
http://<你的NAS_IP>:5888/vod.json
```
保存后，首选源将自动变为：
> 🎬 **我的私有影库 [全网聚合·多线秒播]**

### 方式二：局域网 ADB 一键推送（推荐）

如果电视盒子开启了 ADB 网络调试：
```bash
# 修改 push_to_box.sh 中的 BOX_IP (如 192.168.0.245)
./push_to_box.sh
```
脚本将自动秒连机顶盒并静默配置生效。

---

## ⏰ 生产级自动化部署 (systemd)

项目自带 systemd 服务与定时器配置，支持系统自启与每日定时更新：

```bash
# 复制服务文件至用户 systemd 目录
mkdir -p ~/.config/systemd/user
cp systemd/tvbox-vod.service ~/.config/systemd/user/
cp systemd/tvbox-sync.service ~/.config/systemd/user/
cp systemd/tvbox-sync.timer ~/.config/systemd/user/

# 重载并启用服务与定时器
systemctl --user daemon-reload
systemctl --user enable --now tvbox-vod.service
systemctl --user enable --now tvbox-sync.timer

# 查看每日凌晨 5 点定时器状态
systemctl --user list-timers
```

---

## 📄 开源许可

本项目基于 [MIT](LICENSE) 协议发布，仅供个人家庭局域网内影视资料整理与学习交流使用。
