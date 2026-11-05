# 智能设备产测工具

统一入口，提供主控设备、智能门控、智能室内/室外音箱产测、音箱序列号比对和扫码打印工具。

| 工具 | 主要功能 |
| --- | --- |
| 主控 | 设备发现、SSH、多设备检测、芯片差异化测试、MAC 写入、标签打印、记录导出与上传 |
| 门控 | 设备发现、MAC 写入、遥控器配对、应急开关、无线检测、OTA、标签打印 |
| 音箱 | 多窗口测试、MAC 写入、音麦与无线检测、红外／微波检测、固件升级、室外 RTSP 视频 |
| 音箱序列号比对 | 自动发现室内/室外音箱，核对设备 SN、机身 SN 与彩盒贴纸 SN |
| 扫码打印 | 扫码解析与标签打印 |

## 启动与打包

在工程根目录执行：

```bash
pip install -r requirements.txt
python main.py
```

所有工具统一使用本工程的打包脚本，在目标操作系统上执行：

```bash
python build.py
```

产物位于 `dist/`，名称为 `智能设备产测工具_<版本号>`（Windows 为 `.exe`，macOS 同时生成 `.app`）。`config/`、`certs/` 随程序分发；共享字体嵌入程序。脚本支持重复打包，覆盖同版本产物。

## 项目结构

```text
doorCheck/
├── main.py                         # 四种产测模式的统一入口
├── build.py                        # 统一打包脚本
├── requirements.txt                # Python 依赖
├── config/
│   ├── config.yaml                 # 门控、音箱配置及应用版本
│   ├── printer/
│   │   └── config.yaml             # 扫码打印工具配置
│   └── master_control/
│       ├── config.yaml             # 主控独立配置
│       ├── blescan.sh              # 主控蓝牙检测脚本
│       └── slescan.sh              # 主控星闪检测脚本
├── assets/fonts/label.ttf          # 各工具共用标签字体
├── certs/                          # SSL 证书
├── src/
│   ├── ui/                         # 模式选择、门控及音箱界面，含 icon/
│   ├── core/                       # 门控及音箱测试引擎、协议
│   ├── network/                    # mDNS、MQTT、HTTP、TFTP 等通信
│   ├── hardware/                   # 门控及音箱标签打印
│   ├── data/                       # 门控及音箱数据存储代码
│   ├── utils/                      # 配置、日志、路径、串口等工具
│   ├── qrcode_print/               # 扫码打印独立模块（界面、模板、打印、配置及测试）
│   └── master_control/             # 主控独立模块
│       ├── bootstrap.py            # 崩溃捕获
│       ├── gui/                    # 主控界面
│       ├── core/                   # 发现、SSH、芯片配置、检测及数据库
│       └── utils/                  # 主控配置、打印、导出及上传
├── tools/
│   ├── master_control/             # 主控冒烟测试、打印及发现调试
│   ├── simulator/                  # 门控模拟器
│   ├── sle/                        # 星闪调试脚本
│   └── …                           # 门控控制、压力测试及打印调试
├── mac_pool.csv                    # 门控、音箱 MAC 池
├── test_records.db                 # 门控数据库（运行时生成）
├── data/                           # 音箱数据库（运行时生成）
├── logs/                           # 统一日志及主控运行数据
│   ├── smart_YYYYMMDD.log          # 门控、音箱日志
│   ├── mcCheck_YYYYMMDD.log        # 主控日志
│   ├── crash.log                   # 主控崩溃日志
│   ├── mc_checker.db               # 主控数据库
│   ├── mac_pool.csv                # 可选的主控专用 MAC 池
│   └── reports/                    # 主控打印输出（按需生成）
└── dist/                           # 打包产物（构建时生成）
```

## 配置与数据

门控、音箱使用 `config/config.yaml`；扫码打印使用 `config/printer/config.yaml`；主控使用 `config/master_control/config.yaml`，支持配置热加载。各工具保持独立的业务模块、打印配置和数据库，字体与日志目录共用。

所有工具的应用版本号及打包版本统一在 `config/config.yaml` 的 `app.version` 中维护。

打包后，运行数据位于程序旁；macOS 的 `.app` 与同目录可执行文件共用外置配置和 `logs/`。沿用旧主控历史记录时，关闭程序后将 `mc_checker.db` 复制到 `logs/mc_checker.db`。主控离线 MAC 池使用 `logs/mac_pool.csv`，与门控、音箱的池文件分开。

## 离线验证

```bash
python tools/master_control/smoke_clear_and_version.py
python tools/master_control/smoke_scan_entry.py
```

上述验证不连接真实设备、不打印。设备检测、MAC 写入、上传和物理打印需在产线实机验收。

## 主控检测说明

已迁移原始主控工程 `mcChecker` 的提交 `c8de99ff3b584e973b7a8f399c2a87427f029016`（`usb_ble_fix`），保留本工程的独立主控配置、共享日志与统一版本号。

- USB 接口：RK3576 使用 Type-A，海思 310P 使用 Type-C。测试前接入 USB 3.0 设备；读取 `lsusb -t`，不可用时退回 sysfs。默认协商速率至少 5000 Mbps 才通过，不将 root hub 自身计为设备。USB 已接入单项检测、一键测试、打印门限、历史记录和上传。
- USB 配置：在 `config/master_control/config.yaml` 的 `usb` 节点设置 `min_speed_mbps`、`probe_retries` 和 `probe_interval`；芯片的 `usb_detection`、`usb_connector` 分别控制检测开关与接口名称。
- 内置硬盘：发现设备与硬盘检测共用识别逻辑，排除 USB/可移动存储，优先显示 NVMe、SATA，再到 eMMC。只接 U 盘时不会将 U 盘作为内置硬盘，卡片明确提示缺少内置盘。
- 蓝牙：扫描前停止设备上占用蓝牙的 Java 进程并重启蓝牙服务，缩短扫描等待时间，增加带时间戳的阶段日志，并调整主控侧脚本等待超时。此操作会中断设备上的 Java 业务服务，需在产测环境执行。
- 电池：读取厂商名，按品牌选择 shutdown 地址字；内置格瑞普 Greenway 和九夷 Sunwoda 配置。未知品牌不猜测地址；可用 `battery.shutdown_profiles` 替换内置表，或用 `battery.force_profile` 指定已知批次。写入 Auto Ship 和关机时间后回读校验，配置失败会使电池检测失败；海思 310P 仍按芯片配置跳过 shutdown 设置。

电池寄存器写入、USB 实际协商速率和蓝牙扫描需在对应芯片与电池品牌的实机上验收。
