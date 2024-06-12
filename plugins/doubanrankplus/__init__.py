import datetime
import re
import xml.dom.minidom
from threading import Event
from typing import Tuple, List, Dict, Any

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import schemas
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.subscribe import SubscribeChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaType
from app.utils.dom import DomUtils
from app.utils.http import RequestUtils


class DoubanRankPlus(_PluginBase):
    # 插件名称
    plugin_name = "豆瓣榜单订阅Plus"
    # 插件描述
    plugin_desc = "自动订阅豆瓣热门榜单。增加自定义保存路径，全季度订阅，上映年份过滤。"
    # 插件图标
    plugin_icon = "movie.jpg"
    # 插件版本
    plugin_version = "0.0.1"
    # 插件作者
    plugin_author = "jxxghp,boeto"
    # 作者主页
    author_url = "https://github.com/boeto/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "doubanrankplus_"
    # 加载顺序
    plugin_order = 7
    # 可使用的用户级别
    auth_level = 2

    # 退出事件
    _event = Event()
    # 私有属性
    downloadchain: DownloadChain = None
    subscribechain: SubscribeChain = None
    mediachain: MediaChain = None
    _scheduler = None
    _douban_address = {
        "movie-ustop": "https://rsshub.app/douban/movie/ustop",
        "movie-weekly": "https://rsshub.app/douban/movie/weekly",
        "movie-real-time": "https://rsshub.app/douban/movie/weekly/movie_real_time_hotest",
        "show-domestic": "https://rsshub.app/douban/movie/weekly/show_domestic",
        "movie-hot-gaia": "https://rsshub.app/douban/movie/weekly/movie_hot_gaia",
        "tv-hot": "https://rsshub.app/douban/movie/weekly/tv_hot",
        "movie-top250": "https://rsshub.app/douban/movie/weekly/movie_top250",
        "movie-top250-full": "https://rsshub.app/douban/list/movie_top250",
    }
    _enabled = False
    _cron = ""
    _onlyonce = False
    _rss_addrs = []
    _ranks = []
    _vote = 0
    _clear = False
    _clearflag = False
    _proxy = False
    _is_seasons_all = False
    _release_year = 0

    def init_plugin(self, config: dict = None):
        self.downloadchain = DownloadChain()
        self.subscribechain = SubscribeChain()
        self.mediachain = MediaChain()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._proxy = config.get("proxy")
            self._onlyonce = config.get("onlyonce")

            self._is_seasons_all = config.get("is_seasons_all", False)
            self._release_year = (
                int(config.get("release_year", 0)) if config.get("release_year") else 0
            )

            self._vote = float(config.get("vote")) if config.get("vote") else 0
            rss_addrs = config.get("rss_addrs")
            if rss_addrs:
                if isinstance(rss_addrs, str):
                    self._rss_addrs = rss_addrs.split("\n")
                else:
                    self._rss_addrs = rss_addrs
            else:
                self._rss_addrs = []
            self._ranks = config.get("ranks") or []
            self._clear = config.get("clear")

        # 停止现有任务
        self.stop_service()

        # 启动服务
        if self._enabled or self._onlyonce:
            if self._onlyonce:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("豆瓣榜单订阅Plus服务启动，立即运行一次")
                self._scheduler.add_job(
                    func=self.__refresh_rss,
                    trigger="date",
                    run_date=datetime.datetime.now(tz=pytz.timezone(settings.TZ))
                    + datetime.timedelta(seconds=3),
                )

                if self._scheduler.get_jobs():
                    # 启动服务
                    self._scheduler.print_jobs()
                    self._scheduler.start()

            if self._onlyonce or self._clear:
                # 关闭一次性开关
                self._onlyonce = False
                # 记录缓存清理标志
                self._clearflag = self._clear
                # 关闭清理缓存
                self._clear = False
                # 保存配置
                self.__update_config()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
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
                "path": "/delete_history",
                "endpoint": self.delete_history,
                "methods": ["GET"],
                "summary": "删除豆瓣榜单订阅Plus历史记录",
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            return [
                {
                    "id": "DoubanRankPlus",
                    "name": "豆瓣榜单订阅Plus服务",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.__refresh_rss,
                    "kwargs": {},
                }
            ]
        elif self._enabled:
            return [
                {
                    "id": "DoubanRankPlus",
                    "name": "豆瓣榜单订阅Plus服务",
                    "trigger": CronTrigger.from_crontab("0 8 * * *"),
                    "func": self.__refresh_rss,
                    "kwargs": {},
                }
            ]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "proxy",
                                            "label": "使用代理服务器",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
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
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "is_seasons_all",
                                            "label": "订阅剧集全季度",
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "cron",
                                            "label": "执行周期",
                                            "placeholder": "5位cron表达式，留空自动",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "vote",
                                            "label": "评分",
                                            "placeholder": "评分大于等于该值才订阅",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "release_year",
                                            "label": "上映年份",
                                            "placeholder": "年份大于等于该值才订阅",
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
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "chips": True,
                                            "multiple": True,
                                            "model": "ranks",
                                            "label": "热门榜单",
                                            "items": [
                                                {
                                                    "title": "电影北美票房榜",
                                                    "value": "movie-ustop",
                                                },
                                                {
                                                    "title": "一周口碑电影榜",
                                                    "value": "movie-weekly",
                                                },
                                                {
                                                    "title": "实时热门电影",
                                                    "value": "movie-real-time",
                                                },
                                                {
                                                    "title": "热门综艺",
                                                    "value": "show-domestic",
                                                },
                                                {
                                                    "title": "热门电影",
                                                    "value": "movie-hot-gaia",
                                                },
                                                {
                                                    "title": "热门电视剧",
                                                    "value": "tv-hot",
                                                },
                                                {
                                                    "title": "电影TOP10",
                                                    "value": "movie-top250",
                                                },
                                                {
                                                    "title": "电影TOP250",
                                                    "value": "movie-top250-full",
                                                },
                                            ],
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "rss_addrs",
                                            "label": "自定义榜单地址",
                                            "placeholder": "每行一个地址。地址后加分号 `;`，自定义地址的下载路径。分号后用#按类型分割下载路径/电影#/电视剧#/动漫，\nhttps://rsshub.app/douban/movie/ustop\nhttps://rsshub.app/douban/movie/ustop;/download_to_path\nhttps://rsshub.app/douban/doulist/44852852;/movie_path#/tv_path#/anime_path",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "clear",
                                            "label": "清理历史记录",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "cron": "",
            "proxy": False,
            "onlyonce": False,
            "vote": "",
            "ranks": [],
            "rss_addrs": "",
            "clear": False,
        }

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        # 查询历史记录
        historys = self.get_data("history")
        if not historys:
            return [
                {
                    "component": "div",
                    "text": "暂无数据",
                    "props": {
                        "class": "text-center",
                    },
                }
            ]
        # 数据按时间降序排序
        historys = sorted(historys, key=lambda x: x.get("time"), reverse=True)
        # 拼装页面
        contents = []
        for history in historys:
            title = history.get("title")
            poster = history.get("poster")
            mtype = history.get("type")
            time_str = history.get("time")
            doubanid = history.get("doubanid")
            contents.append(
                {
                    "component": "VCard",
                    "content": [
                        {
                            "component": "VDialogCloseBtn",
                            "props": {
                                "innerClass": "absolute top-0 right-0",
                            },
                            "events": {
                                "click": {
                                    "api": "plugin/DoubanRankPlus/delete_history",
                                    "method": "get",
                                    "params": {
                                        "key": f"doubanrankplus: {title} (DB:{doubanid})",
                                        "apikey": settings.API_TOKEN,
                                    },
                                }
                            },
                        },
                        {
                            "component": "div",
                            "props": {
                                "class": "d-flex justify-space-start flex-nowrap flex-row",
                            },
                            "content": [
                                {
                                    "component": "div",
                                    "content": [
                                        {
                                            "component": "VImg",
                                            "props": {
                                                "src": poster,
                                                "height": 120,
                                                "width": 80,
                                                "aspect-ratio": "2/3",
                                                "class": "object-cover shadow ring-gray-500",
                                                "cover": True,
                                            },
                                        }
                                    ],
                                },
                                {
                                    "component": "div",
                                    "content": [
                                        {
                                            "component": "VCardTitle",
                                            "props": {
                                                "class": "ps-1 pe-5 break-words whitespace-break-spaces"
                                            },
                                            "content": [
                                                {
                                                    "component": "a",
                                                    "props": {
                                                        "href": f"https://movie.douban.com/subject/{doubanid}",
                                                        "target": "_blank",
                                                    },
                                                    "text": title,
                                                }
                                            ],
                                        },
                                        {
                                            "component": "VCardText",
                                            "props": {"class": "pa-0 px-2"},
                                            "text": f"类型：{mtype}",
                                        },
                                        {
                                            "component": "VCardText",
                                            "props": {"class": "pa-0 px-2"},
                                            "text": f"时间：{time_str}",
                                        },
                                    ],
                                },
                            ],
                        },
                    ],
                }
            )

        return [
            {
                "component": "div",
                "props": {
                    "class": "grid gap-3 grid-info-card",
                },
                "content": contents,
            }
        ]

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

    def delete_history(self, key: str, apikey: str):
        """
        删除同步历史记录
        """
        if apikey != settings.API_TOKEN:
            return schemas.Response(success=False, message="API密钥错误")
        # 历史记录
        historys = self.get_data("history")
        if not historys:
            return schemas.Response(success=False, message="未找到历史记录")
        # 删除指定记录
        historys = [h for h in historys if h.get("unique") != key]
        self.save_data("history", historys)
        return schemas.Response(success=True, message="删除成功")

    def __update_config(self):
        """
        列新配置
        """
        self.update_config(
            {
                "enabled": self._enabled,
                "cron": self._cron,
                "onlyonce": self._onlyonce,
                "vote": self._vote,
                "ranks": self._ranks,
                "rss_addrs": "\n".join(map(str, self._rss_addrs)),
                "clear": self._clear,
                "is_seasons_all": self._is_seasons_all,
                "release_year": self._release_year,
            }
        )

    def __get_addr_save_paths(addr: str) -> Dict[str, Dict[str, str] | str | None]:
        # 提取分号分割的链接和保存地址
        if ";" not in addr:
            return {"addr": addr, "customize_save_path": None}
        else:
            logger.debug("分割订阅地址")
            split_str = addr.split(";")
            str_list: List[str] = []
            for item in split_str:
                if item.strip():
                    str_list.append(item.strip())
            addr = str_list[0]
            customize_save_path = str_list[1]

            logger.debug(f"addr: {addr}")
            logger.debug("customize_save_path:" f" {customize_save_path}")

            if "#" in customize_save_path:
                customize_save_path_list = customize_save_path.split("#")

                logger.debug("customize_save_path_list:" f" {customize_save_path_list}")

                customize_save_path_movie = customize_save_path_list[0]
                customize_save_path_tv = customize_save_path_list[1]
                customize_save_path_anime = (
                    customize_save_path_list[2] or customize_save_path_tv
                )

                logger.debug(
                    f"订阅链接 {addr} 的自定义保存路径为:"
                    f" 电影:{customize_save_path_movie},"
                    f" 电视剧: {customize_save_path_tv},"
                    f" 动漫: {customize_save_path_anime}"
                )

            else:
                customize_save_path_movie = customize_save_path
                customize_save_path_tv = customize_save_path
                customize_save_path_anime = customize_save_path

                logger.debug(
                    f"订阅链接 {addr} 的自定义保存路径为:" f" {customize_save_path}"
                )
            customize_save_paths = {
                "movie": customize_save_path_movie,
                "tv": customize_save_path_tv,
                "anime": customize_save_path_anime,
            }
            return {"addr": addr, "customize_save_paths": customize_save_paths}

    def __refresh_rss(self):
        """
        刷新RSS
        """
        logger.info(f"开始刷新豆瓣榜单Plus ...")
        addr_list = self._rss_addrs + [
            self._douban_address.get(rank) for rank in self._ranks
        ]
        if not addr_list:
            logger.info(f"未设置榜单RSS地址")
            return
        else:
            logger.info(f"共 {len(addr_list)} 个榜单RSS地址需要刷新")

        # 读取历史记录
        if self._clearflag:
            history = []
        else:
            history: List[dict] = self.get_data("history") or []

        for _addr in addr_list:
            if not _addr:
                continue
            try:
                logger.info(f"获取RSS：{_addr} ...")
                addr_result = DoubanRankPlus.__get_addr_save_paths(_addr)
                addr = addr_result.get("addr")
                customize_save_paths = addr_result.get("customize_save_paths")
                logger.debug(f"addr: {addr}")
                logger.debug(f"customize_save_paths: {customize_save_paths}")

                rss_infos = self.__get_rss_info(addr)
                if not rss_infos:
                    logger.error(f"RSS地址：{addr} ，未查询到数据")
                    continue
                else:
                    logger.info(f"RSS地址：{addr} ，共 {len(rss_infos)} 条数据")

                for rss_info in rss_infos:
                    if self._event.is_set():
                        logger.info(f"订阅服务停止")
                        return
                    mtype = None
                    logger.debug(f"rss_info:::{rss_info}")
                    title = rss_info.get("title")
                    douban_id = rss_info.get("doubanid")
                    year = rss_info.get("year")
                    type_str = rss_info.get("type")
                    if type_str == "movie":
                        mtype = MediaType.MOVIE
                    elif type_str:
                        mtype = MediaType.TV
                    unique_flag = f"doubanrankplus: {title} (DB:{douban_id})"

                    if unique_flag in [
                        h.get("unique") for h in history if h is not None
                    ]:
                        continue

                    logger.debug(f"开始处理:::{title} ({year})")
                    # 元数据
                    meta = MetaInfo(title)
                    meta.year = year
                    if mtype:
                        meta.type = mtype
                    # 识别媒体信息
                    tmdbinfo = None
                    if douban_id:
                        # 识别豆瓣信息
                        logger.debug(
                            f"开始通过豆瓣ID获取TMDB信息::: douban_id:::{douban_id}, meta.type:::{meta.type}"
                        )
                        if settings.RECOGNIZE_SOURCE == "themoviedb":
                            tmdbinfo = self.mediachain.get_tmdbinfo_by_doubanid(
                                doubanid=douban_id, mtype=meta.type
                            )

                            if not tmdbinfo:
                                logger.warn(
                                    f"未能通过豆瓣ID {douban_id} 获取到TMDB信息，标题：{title}，豆瓣ID：{douban_id}"
                                )
                                continue

                            tmdbinfo_media_type = tmdbinfo.get("media_type")
                            logger.debug(
                                f'通过豆瓣ID {douban_id} 获取到TMDB信息::: tmdbinfo.id: {tmdbinfo.get("id")},tmdbinfo.media_type: {tmdbinfo_media_type}'
                            )

                            mediainfo = self.chain.recognize_media(
                                meta=meta,
                                tmdbid=tmdbinfo.get("id"),
                                mtype=tmdbinfo_media_type,
                            )
                            if not mediainfo:
                                logger.warn(
                                    f'TMDBID {tmdbinfo.get("id")} 未识别到媒体信息'
                                )
                                continue
                        else:
                            mediainfo = self.chain.recognize_media(
                                meta=meta, doubanid=douban_id
                            )
                            if not mediainfo:
                                logger.warn(f"豆瓣ID {douban_id} 未识别到媒体信息")
                                continue
                    else:
                        # 匹配媒体信息
                        mediainfo: MediaInfo = self.chain.recognize_media(meta=meta)
                        if not mediainfo:
                            logger.warn(
                                f"未识别到媒体信息，标题：{title}，豆瓣ID：{douban_id}"
                            )
                            continue

                    # 保存路径
                    save_path = None
                    if customize_save_paths:
                        if mediainfo.type == MediaType.TV:
                            save_path = customize_save_paths["tv"]
                        elif mediainfo.type == MediaType.MOVIE:
                            save_path = customize_save_paths["movie"]

                    is_added = False
                    number_of_seasons = tmdbinfo.get("number_of_seasons")
                    logger.debug(f"number_of_seasons:::{number_of_seasons}")
                    # 如果是剧集且开启全季订阅，则轮流下载每一季
                    if (
                        self._is_seasons_all
                        and mediainfo.type == MediaType.TV
                        and number_of_seasons
                    ):

                        genre_ids = tmdbinfo.get("genre_ids")
                        ANIME_GENRE_ID = 16
                        if ANIME_GENRE_ID in genre_ids:
                            save_path = customize_save_paths["anime"]
                        # if number_of_seasons > 1:
                        for i in range(1, number_of_seasons + 1):
                            logger.debug(
                                f"开始添加全季订阅:"
                                f"{mediainfo.title} ({mediainfo.year})"
                                f" 第{i}/{number_of_seasons}季"
                            )
                            is_added = self.__checke_and_add_subscribe(
                                meta=meta,
                                mediainfo=mediainfo,
                                season=i,
                                save_path=save_path,
                            )
                    else:
                        is_added = self.__checke_and_add_subscribe(
                            meta=meta,
                            mediainfo=mediainfo,
                            season=meta.begin_season,
                            save_path=save_path,
                        )

                    # 存储历史记录
                    if is_added:
                        history_payload = {
                            "title": title,
                            "type": mediainfo.type.value,
                            "year": mediainfo.year,
                            "poster": mediainfo.get_poster_image(),
                            "overview": mediainfo.overview,
                            "tmdbid": mediainfo.tmdb_id,
                            "doubanid": douban_id,
                            "time": datetime.datetime.now().strftime(
                                "%Y-%m-%d %H:%M:%S"
                            ),
                            "unique": unique_flag,
                        }
                        logger.info(f"已添加订阅：{history_payload}")
                        history.append(history_payload)

            except Exception as e:
                logger.error(str(e))

        # 保存历史记录
        self.save_data("history", history)
        # 缓存只清理一次
        self._clearflag = False
        logger.info("所有榜单RSS刷新完成")

    def __checke_and_add_subscribe(
        self,
        meta,
        mediainfo,
        season,
        save_path,
    ) -> bool:
        if save_path:
            logger.info(f"{mediainfo.title_year} 的自定义保存路径为: {save_path}")

        # 判断上映年份是否符合要求
        if self._release_year and int(mediainfo.year) < self._release_year:
            logger.info(
                f"{mediainfo.title_year} 上映年份: {mediainfo.year}, 不符合要求"
            )
            return False
        # 判断评分是否符合要求
        if self._vote and mediainfo.vote_average < self._vote:
            logger.info(
                f"{mediainfo.title_year} 评分: {mediainfo.vote_average}, 不符合要求"
            )
            return False

        # 查询缺失的媒体信息
        exist_flag, _ = self.downloadchain.get_no_exists_info(
            meta=meta, mediainfo=mediainfo
        )
        if exist_flag:
            logger.info(f"{mediainfo.title_year} 媒体库中已存在")
            return False

        # 判断用户是否已经添加订阅
        if self.subscribechain.exists(mediainfo=mediainfo, meta=meta):
            logger.info(f"{mediainfo.title_year} 订阅已存在")
            return False

        # 添加订阅
        self.subscribechain.add(
            title=mediainfo.title,
            year=mediainfo.year,
            mtype=mediainfo.type,
            tmdbid=mediainfo.tmdb_id,
            season=season,
            exist_ok=True,
            username="豆瓣榜单Plus",
            save_path=save_path,
        )
        return True

    def __get_rss_info(self, addr) -> List[dict]:
        """
        获取RSS
        """
        try:
            if self._proxy:
                ret = RequestUtils(proxies=settings.PROXY).get_res(addr)
            else:
                ret = RequestUtils().get_res(addr)
            if not ret:
                return []
            ret_xml = ret.text
            ret_array = []
            # 解析XML
            dom_tree = xml.dom.minidom.parseString(ret_xml)
            rootNode = dom_tree.documentElement
            items = rootNode.getElementsByTagName("item")
            for item in items:
                try:
                    rss_info = {}
                    # 标题
                    title = DomUtils.tag_value(item, "title", default="")
                    # 链接
                    link = DomUtils.tag_value(item, "link", default="")
                    # 年份
                    description = DomUtils.tag_value(item, "description", default="")
                    # 类型
                    itemtype = DomUtils.tag_value(item, "type", default="")

                    if not title and not link:
                        logger.warn(f"条目标题和链接均为空，无法处理")
                        continue
                    rss_info["title"] = title
                    rss_info["link"] = link
                    rss_info["type"] = itemtype

                    doubanid = re.findall(r"/(\d+)/", link)
                    if doubanid:
                        doubanid = doubanid[0]
                    if doubanid and not str(doubanid).isdigit():
                        logger.warn(f"解析的豆瓣ID格式不正确：{doubanid}")
                        continue
                    rss_info["doubanid"] = doubanid

                    # 匹配4位独立数字1900-2099年
                    year = re.findall(r"\b(19\d{2}|20\d{2})\b", description)
                    if year:
                        rss_info["year"] = year[0]

                    # 返回对象
                    ret_array.append(rss_info)
                except Exception as e1:
                    logger.error("解析RSS条目失败：" + str(e1))
                    continue
            return ret_array
        except Exception as e:
            logger.error("获取RSS失败：" + str(e))
            return []
