# 智能设备产测工具

统一入口，依次提供主控设备、智能门控、智能室内音箱和智能室外音箱产测。

| 工具 | 主要功能 |
| --- | --- |
| 主控 | 设备发现、SSH、多设备检测、芯片差异化测试、MAC 写入、标签打印、记录导出与上传 |
| 门控 | 设备发现、MAC 写入、遥控器配对、应急开关、无线检测、OTA、标签打印 |
| 音箱 | 多窗口测试、MAC 写入、音麦与无线检测、红外／微波检测、固件升级、室外 RTSP 视频 |

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

门控、音箱使用 `config/config.yaml`；主控使用 `config/master_control/config.yaml`，支持配置热加载。各工具保持独立的业务模块、打印配置和数据库，字体与日志目录共用。

所有工具的应用版本号及打包版本统一在 `config/config.yaml` 的 `app.version` 中维护。

打包后，运行数据位于程序旁；macOS 的 `.app` 与同目录可执行文件共用外置配置和 `logs/`。沿用旧主控历史记录时，关闭程序后将 `mc_checker.db` 复制到 `logs/mc_checker.db`。主控离线 MAC 池使用 `logs/mac_pool.csv`，与门控、音箱的池文件分开。

## 离线验证

```bash
python tools/master_control/smoke_clear_and_version.py
python tools/master_control/smoke_scan_entry.py
```

上述验证不连接真实设备、不打印。设备检测、MAC 写入、上传和物理打印需在产线实机验收。
