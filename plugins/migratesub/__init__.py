from typing import Any, Dict, Tuple, List
import datetime
import pytz
from app.db.site_oper import SiteOper
from app.db.subscribe_oper import SubscribeOper
from apscheduler.schedulers.background import BackgroundScheduler
from threading import Event

from app.db.models.site import Site
from app.db.models.subscribe import Subscribe
from app.plugins import _PluginBase
from app.core.config import settings
from app import schemas
from app.log import logger
from app.utils.http import RequestUtils


class MigrateSub(_PluginBase):
    # 插件名称
    plugin_name = "迁移订阅"
    # 插件描述
    plugin_desc = "迁移旧MP的订阅配置到新MP"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/boeto/MoviePilot-Plugins/main/icons/EpisodeNoExist.png"
    # 插件版本
    plugin_version = "0.0.1"
    # 插件作者
    plugin_author = "boeto"
    # 作者主页
    author_url = "https://github.com/boeto/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "migratesub_"
    # 加载顺序
    plugin_order = 6
    # 可使用的用户级别
    auth_level = 2

    # 退出事件
    _event = Event()

    # 私有属性
    _plugin_id = "MigrateSub"
    _scheduler = None
    _subscribeoper: SubscribeOper
    _siteOper: SiteOper

    _enabled: bool = False
    _onlyonce: bool = False
    _is_with_sites: bool = False

    _migrate_from_url: str = ""
    _migrate_api_token: str = ""

    def init_plugin(self, config: dict[str, Any] | None = None):
        logger.debug(f"初始化插件 {self.plugin_name}: {config}")
        self.__setup(config)
        # if hasattr(settings, "VERSION_FLAG"):
        #     version = settings.VERSION_FLAG  # V2
        # else:
        #     version = "v1"

        # if version == "v2":
        #     self.setup_v2()
        # else:
        #     self.setup_v1()

    def __setup(self, config: dict[str, Any] | None = None):
        # 初始化逻辑
        self._subscribeoper = SubscribeOper()
        self._siteOper = SiteOper()

        if config:
            self._enabled = config.get("enabled", False)
            self._onlyonce = config.get("onlyonce", False)
            self._is_with_sites = config.get("is_with_sites", False)
            self._migrate_api_token = config.get("migrate_api_token", "")
            self._migrate_from_url = config.get("migrate_from_url", "")

        # 停止现有任务
        self.stop_service()

        # 启动服务
        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info(f"{self.plugin_name}服务启动, 立即运行一次")
                self._scheduler.add_job(
                    func=self.__start_migrate,
                    trigger="date",
                    run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                    + datetime.timedelta(seconds=3),
                )

                if self._scheduler.get_jobs():
                    # 启动服务
                    self._scheduler.print_jobs()
                    self._scheduler.start()

            if self._onlyonce:
                # 关闭一次性开关
                self.__update_onlyonce(False)

    def __update_config(self):
        """
        更新配置
        """
        __config = {
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "is_with_sites": self._is_with_sites,
            "migrate_api_token": self._migrate_api_token,
            "migrate_from_url": self._migrate_from_url.rstrip("/"),
        }
        logger.debug(f"更新配置 {__config}")
        self.update_config(__config)

    def __start_migrate(self):
        """
        启动迁移
        """
        if not self._migrate_api_token:
            logger.error("未设置迁移Token，结束迁移")
            return
        if not self._migrate_from_url:
            logger.error("未设置迁移url，结束迁移")
            return

        logger.info("开始获取订阅 ...")
        ret_sub_list = self.__get_migrate_sub_list()

        if not ret_sub_list:
            logger.warn("没有需要添加的订阅，结束迁移")
            return
        else:
            logger.info("获取到原MP订阅列表，开始添加订阅")
            add_count = 0
            # deal_count = 0

            for item in ret_sub_list:
                # 新增订阅
                (isAdded, msg) = self.__add_sub(item)
                logger.debug(f"添加订阅结果：{isAdded}")
                # deal_count += 1
                if isAdded:
                    add_count += 1
                logger.info(msg)
                # if deal_count == 20:
                #     break
            logger.info("订阅迁移完成，共添加 %s 条" % add_count)

            if self._is_with_sites and add_count > 0:
                self.__migrate_sites()

        logger.info("迁移结束")

    def __update_onlyonce(self, enabled: bool):
        self._onlyonce = enabled
        self.__update_config()

    def __migrate_sites(self):
        logger.info("开始获取站点管理 ...")
        ret_sites = self.__get_migrate_sites()
        logger.info("获取站点管理完成")

        if not ret_sites:
            logger.warn("没有需要添加的站点管理，结束迁移")
            return
        else:
            # 清空站点管理
            logger.info("重置新MP站点管理...")
            Site.reset(self._siteOper._db)

            site_count = 0
            # 新增站点
            for item in ret_sites:
                logger.debug(f"开始迁移站点：{item}")
                logger.debug(f"开始迁移站点：{item.get('name')}")
                site = Site(**item)
                site.create(self._siteOper._db)
                site_count += 1
            logger.info("站点迁移完成，共添加 %s 条" % site_count)

    def setup_v2(self):
        # V2版本特有的初始化逻辑
        pass

    def setup_v1(self):
        # V1版本特有的初始化逻辑
        pass

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command():
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """
        获取插件API
        [{
            "path": "/xx",
            "endpoint": self.xxx,
            "methods": ["GET", "POST"],
            "summary": "API说明"
        }]
        """
        return [
            {
                "path": "/sites",
                "endpoint": self.get_sites_list,
                "methods": ["GET"],
                "summary": "获取 所有站点管理",
            },
        ]

    def get_sites_list(self, migrate_api_token: str):
        """
        获取所有站点列表
        """
        logger.debug("获取所有站点列表...")
        if migrate_api_token != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        return self._siteOper.list()

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "migrate_from_url",
                                            "label": "原MP地址: 例如 http://mp.com:3001",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "migrate_api_token",
                                            "label": "原MP API Token",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 6, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "is_with_sites",
                                            "label": "迁移站点管理",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 6, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "onlyonce",
                                            "label": "立即运行一次",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                        },
                                        "content": [
                                            {
                                                "component": "span",
                                                "text": "如果是将原MP迁移订阅到此MP，需要填写原MP Url地址和原MP API Token（当然你原MP肯定是需要同时运行）。开启插件并立即运行一次将会迁移订阅列表。是否需要开启“迁移订阅站点管理”选项，请认真阅读下面的说明。",
                                            }
                                        ],
                                    },
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                        },
                                        "content": [
                                            {
                                                "component": "span",
                                                "text": "新MP开启“迁移订阅站点管理”选项需在原MP同时安装并启用此插件（仅在原MP开启“启用插件”选项即可，不需要填写或开启其他选项）。新MP开启此选项，将在成功迁移所有订阅之后重置新MP“站点管理”中已存在的站点！会重置新MP“站点管理”中已存在的站点！会重置新MP“站点管理”中已存在的站点！这样才能匹配上订阅中的“订阅站点”选项。如果不迁移站点管理，迁移的订阅中将不保留“订阅站点”选项，请按需开启",
                                            }
                                        ],
                                    },
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "is_with_sites": False,
            "migrate_from_url": "",
            "migrate_api_token": "",
        }

    def get_page(self):
        pass

    def stop_service(self):
        """
        停止服务
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))

    def __add_sub(self, item: dict) -> tuple[bool, str]:
        """
        添加订阅
        """
        item_name_year = f"{str(item.get('name', ''))}{str(item.get('year', ''))}"
        logger.debug(f"收到订阅：{item}")

        tmdbid = item.get("tmdbid", None)
        doubanid = str(item.get("doubanid", None))
        season = item.get("season", None)

        is_sub_exists = self._subscribeoper.exists(
            tmdbid=tmdbid, doubanid=doubanid, season=season
        )

        # 去除Subscribe 没有的字段
        kwargs = {k: v for k, v in item.items() if hasattr(Subscribe, k)}

        # 未启用站点迁移则去掉订阅站点管理
        if "sites" in kwargs and not self._is_with_sites:
            kwargs.pop("sites", None)

        # 移除特定字段
        fields_to_remove = [
            "id",
        ]
        for field in fields_to_remove:
            kwargs.pop(field, None)

        if not is_sub_exists:
            logger.info(f"{item_name_year} 订阅不存在，开始添加订阅")
            #     sub = Subscribe.exists(
            #     self._subscribeoper._db,
            #     tmdbid=tmdbid,
            #     doubanid=doubanid,
            #     season=season,
            # )
            sub = Subscribe(
                **kwargs,
            )
            sub.create(self._subscribeoper._db)
            return (True, f"{item_name_year}  添加订阅成功")
        else:
            logger.info(f"{item_name_year} 订阅已存在")
            return (False, "订阅已存在，跳过")

    # 好像没什么用
    # def __add_history(self, item: dict):
    #     """
    #     添加完成订阅历史
    #     """
    #     # 去除kwargs中 SubscribeHistory 没有的字段
    #     kwargs = {k: v for k, v in item.items() if hasattr(SubscribeHistory, k)}

    #     # 去掉主键
    #     if "id" in kwargs:
    #         kwargs.pop("id")

    #     # 未启用站点迁移则去掉订阅站点管理
    #     if "sites" in kwargs and not self._is_with_sites:
    #         kwargs.pop("sites", None)

    #     subscribe = SubscribeHistory(**kwargs)
    #     subscribe.create(self._subscribeoper._db)

    def __get_migrate_plugin_api_url(self, endpoint: str) -> str:
        """
        获取插件API URL
        """
        return f"{self._migrate_from_url}/api/v1/plugin/{self._plugin_id}/{endpoint}?migrate_api_token={self._migrate_api_token}"

    def __get_migrate_endpoint_api_url(self, endpoint: str):
        """
        获取插件API URL
        """
        return f"{self._migrate_from_url}/api/v1/{endpoint}?token={self._migrate_api_token}"

    def __get_migrate_info(self, migrate_url: str):
        """
        从原MP API URL获取信息
        """
        res = RequestUtils().request(method="get", url=migrate_url)

        if not res:
            logger.warn("没有获取到原MP信息，请检查原MP地址和API Token是否正确")
            return
        logger.debug(f"获取到原MP res.status_code：{res.status_code}")

        ret = res.json()
        if len(ret) == 0:
            logger.info("没有需要添加的信息")
            return

        return ret

    def __get_migrate_sub_list(self):
        """
        获取订阅列表
        """
        url = self.__get_migrate_endpoint_api_url("subscribe/list")
        logger.debug(f"获取订阅列表：{url}")
        return self.__get_migrate_info(url)

    def __get_migrate_sites(self):
        """
        获取所有站点列表
        """
        url = self.__get_migrate_plugin_api_url("sites")
        logger.debug(f"获取站点列表：{url}")
        return self.__get_migrate_info(url)
