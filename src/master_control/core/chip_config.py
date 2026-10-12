"""
芯片配置模块
根据设备SN识别芯片类型，提供差异化配置
"""
from dataclasses import dataclass
from typing import Optional
from src.master_control.utils.logger import logger
from src.master_control.utils.config import config


@dataclass
class ChipConfig:
    """芯片配置"""
    chip_type: str          # 芯片类型标识
    chip_model: str         # 芯片型号
    poe_count: int          # POE 口数量
    has_hdmi: bool          # 是否有 HDMI 接口
    fan_detection: bool     # 是否执行风扇检测（False表示默认通过）
    wifi_detection: bool    # 是否执行 WiFi 检测（False表示默认通过）
    bt_detection: bool      # 是否执行蓝牙检测（False表示默认通过）
    nearlink_detection: bool  # 是否执行星闪检测（False表示默认通过）
    description: str        # 描述信息
    battery_detection: bool = True  # 是否执行电池检测（False表示默认通过）


class ChipManager:
    """芯片管理器 - 根据SN识别芯片类型并返回配置"""
    
    # 芯片配置缓存
    _chip_configs_cache = None
    
    @classmethod
    def _load_chip_configs(cls):
        """从配置文件加载芯片配置"""
        if cls._chip_configs_cache is not None:
            return cls._chip_configs_cache
        
        chips_config = config.get('chips', {})
        configs = {}
        
        for chip_type, chip_data in chips_config.items():
            configs[chip_type] = ChipConfig(
                chip_type=chip_type,
                chip_model=chip_data.get('chip_model', 'Unknown'),
                poe_count=chip_data.get('poe_count', 14),
                has_hdmi=chip_data.get('has_hdmi', False),
                fan_detection=chip_data.get('fan_detection', True),
                wifi_detection=chip_data.get('wifi_detection', True),
                bt_detection=chip_data.get('bt_detection', True),
                nearlink_detection=chip_data.get('nearlink_detection', True),
                description=chip_data.get('description', f'{chip_type}芯片'),
                battery_detection=chip_data.get('battery_detection', True)
            )
        
        # 兜底配置
        if 'hisi' not in configs:
            configs['hisi'] = ChipConfig(
                chip_type='hisi',
                chip_model='310P',
                poe_count=14,
                has_hdmi=False,
                fan_detection=True,
                wifi_detection=True,
                bt_detection=True,
                nearlink_detection=True,
                description='海思310P芯片（默认配置）'
            )
        
        cls._chip_configs_cache = configs
        logger.info(f"已加载 {len(configs)} 个芯片配置: {list(configs.keys())}")
        return configs
    
    @classmethod
    def detect_chip_type(cls, sn: str) -> str:
        """
        根据设备SN识别芯片类型
        
        Args:
            sn: 设备序列号
            
        Returns:
            芯片类型标识 ('hisi' 或 'wdse')
        """
        if not sn:
            logger.warning("SN为空，默认使用 hisi 芯片配置")
            return 'hisi'
        
        configs = cls._load_chip_configs()
        sn_lower = sn.lower()
        
        # 按 SN 前缀匹配
        for chip_type in configs:
            if sn_lower.startswith(chip_type):
                return chip_type
        
        # 默认为 hisi
        logger.warning(f"SN [{sn}] 未识别到已知前缀，默认使用 hisi 芯片配置")
        return 'hisi'
    
    @classmethod
    def get_chip_config(cls, sn: str) -> ChipConfig:
        """
        获取芯片配置
        
        Args:
            sn: 设备序列号
            
        Returns:
            ChipConfig: 芯片配置对象
        """
        chip_type = cls.detect_chip_type(sn)
        configs = cls._load_chip_configs()
        config_obj = configs.get(chip_type)
        
        if config_obj:
            logger.info(f"[{sn}] 识别芯片类型: {config_obj.description}")
            return config_obj
        else:
            # 降级到默认配置
            logger.error(f"[{sn}] 未找到芯片类型 [{chip_type}] 的配置，使用默认配置")
            return configs['hisi']
    
    @classmethod
    def register_chip_config(cls, chip_type: str, config: ChipConfig):
        """
        注册新的芯片配置（用于动态扩展）
        
        Args:
            chip_type: 芯片类型标识
            config: 芯片配置对象
        """
        configs = cls._load_chip_configs()
        configs[chip_type] = config
        logger.info(f"注册芯片配置: {chip_type} - {config.description}")
