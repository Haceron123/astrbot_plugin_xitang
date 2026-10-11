"""莃棠签到抽卡好感商店插件（AstrBot Star）。

功能：
- 每日签到：获得随机金币与好感度
- 今日运势：随机品味值 / 运气值 / 厄运值 / 抽卡出货率
- 抽卡：单抽 / 五连 / 十连，可抽到传说、史诗、高级、空白
- 商店：使用金币购买抽奖币、糖果、零食、鲜花、小说、蛋糕
- 赠送礼物：赠送礼物提升好感度
- 主人专属指令：打钱
- 群打卡 / 群头衔 / 学说话 等娱乐功能

数据持久化：用户数据存放于 AstrBot 的 data/plugin_data/<插件名>/user_data.json，
符合 AstrBot 插件开发规范（不写入插件自身目录，防止更新/重装时数据丢失）。
"""

import json
import os
import random
import re
import threading
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.star_tools import StarTools

PLUGIN_VERSION = "1.1.7"
DATA_FILENAME = "user_data.json"

# 用户数据的默认结构（所有字段的默认值）
USER_DEFAULT = {
    "金币": 0,
    "签到时限": "",      # 最近一次签到的日期，形如 2026-10-11
    "签到天数": 0,
    "好感": 0,
    "运势日期": "",      # 最近一次测运势的日期
    "品味值": 0,
    "运气值": 0,
    "厄运值": 0,
    "出货率记录": 0,
    "抽奖币": 0,
    "传说": 0,
    "史诗": 0,
    "高级": 0,
    "空白": 0,
    "总抽数": 0,
    "糖果": 0,
    "零食": 0,
    "鲜花": 0,
    "小说": 0,
    "蛋糕": 0,
    "每日金币领取": "",  # 最近一次领取每日金币的日期
    "每日免费抽币": "",  # 最近一次领取每日抽奖币的日期
}

# 抽卡概率表：r <= 2 传说；r <= 10 史诗；r <= 60 高级；其余空白
LEGEND_RATE = 2
EPIC_RATE = 10
RARE_RATE = 60
LEGEND_GOLD = 15000
EPIC_GOLD = 100
RARE_GOLD = 30

# 商店商品定价：(单价, 存入的字段 key)
PRICE_MAP = {
    "抽奖币": (50, "抽奖币"),
    "糖果": (10, "糖果"),
    "袋装零食": (25, "零食"),
    "鲜花": (45, "鲜花"),
    "小说": (60, "小说"),
    "蛋糕": (120, "蛋糕"),
}

# 礼物配置：字段 key -> 每件增加的好感
GIFT_MAP = {
    "糖果": ("糖果", 2),
    "袋装零食": ("零食", 5),
    "鲜花": ("鲜花", 10),
    "小说": ("小说", 12),
    "蛋糕": ("蛋糕", 25),
}

RANDOM_TIPS = [
    "每日签到可以获得①点好感值，主人不要忘记签到喵",
    "赠送礼物是一种快速提升好感度的方法，主人可以试试喵",
]

RANDOM_THANKS = [
    "谢谢主人送来的糖果，我很喜欢喵（开心）",
    "谢谢主人酱",
    "谢谢喵",
]

RANDOM_NO_ITEM = [
    "主人，你没有足够的糖果来赠送（哭哭）",
    "主人酱你…没有足够的糖果",
    "对不起，主人，你没有足够的糖果来赠送（哭哭）",
]


@register("astrbot_plugin_xitang", "莃棠", "签到抽卡好感商店插件", PLUGIN_VERSION)
class XiTangLottery(Star):
    """莃棠签到抽卡好感商店插件。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        # AstrBot 通过 _conf_schema.json 解析插件配置并传入；没有配置时兼容 None
        self.config = config if isinstance(config, dict) else {}
        self.master_qq = str(self.config.get("master_qq", "") or "").strip()
        # 读写锁：防止多个消息事件同时读写数据文件导致丢失更新
        self._lock = threading.RLock()
        self._data: dict | None = None
        self._data_path = self._resolve_data_path()
        self._load_data()

    # ------------------------------------------------------------------ #
    # 数据层                                                              #
    # ------------------------------------------------------------------ #

    def _resolve_data_path(self) -> str:
        """解析数据文件路径：data/plugin_data/<插件名>/user_data.json。"""
        plugin_name = getattr(self, "name", None) or "astrbot_plugin_xitang"
        try:
            data_dir = StarTools.get_data_dir(plugin_name)
        except Exception as e:  # 兜底：极少数情况下无法解析插件名
            logger.warning(f"解析插件数据目录失败，使用默认目录：{e}")
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            data_dir = os.path.join(
                get_astrbot_data_path(),
                "plugin_data",
                plugin_name,
            )
            os.makedirs(data_dir, exist_ok=True)
        return os.path.join(str(data_dir), DATA_FILENAME)

    def _load_data(self) -> None:
        """从磁盘加载用户数据到内存缓存；首次运行会尝试迁移旧数据。"""
        with self._lock:
            if self._data is not None:
                return
            data: dict = {}
            if os.path.exists(self._data_path):
                try:
                    with open(self._data_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning(f"读取用户数据失败，将重新开始：{e}")
                    data = {}
            else:
                # 迁移旧版插件目录下的 user_data.json（如果存在）
                legacy_path = os.path.join(os.path.dirname(__file__), DATA_FILENAME)
                if os.path.exists(legacy_path):
                    try:
                        with open(legacy_path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        logger.info(
                            "检测到旧版 user_data.json，已迁移到 data/plugin_data 目录。"
                        )
                    except (json.JSONDecodeError, OSError) as e:
                        logger.warning(f"旧版 user_data.json 读取失败：{e}")
                        data = {}
            if not isinstance(data, dict):
                data = {}
            self._data = data
            self._save_data()

    def _save_data(self) -> None:
        """将内存缓存写入磁盘。"""
        with self._lock:
            if self._data is None:
                return
            try:
                os.makedirs(os.path.dirname(self._data_path), exist_ok=True)
                with open(self._data_path, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
            except OSError as e:
                logger.error(f"保存用户数据失败：{e}")

    def _get_user(self, sender_id: str) -> dict:
        """获取（必要时创建）某个用户的数据字典。"""
        with self._lock:
            user = self._data.get(sender_id)
            if user is None or not isinstance(user, dict):
                user = dict(USER_DEFAULT)
                self._data[sender_id] = user
            # 补全旧版本可能缺失的字段
            for key, default in USER_DEFAULT.items():
                user.setdefault(key, default)
            return user

    @staticmethod
    def _today(event: AstrMessageEvent) -> str:
        """根据消息时间戳得到本地日期字符串（yyyy-MM-dd）。"""
        ts = getattr(event.message_obj, "timestamp", None)
        if isinstance(ts, (int, float)) and ts > 0:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        return datetime.now().strftime("%Y-%m-%d")

    # ------------------------------------------------------------------ #
    # 抽卡核心逻辑                                                        #
    # ------------------------------------------------------------------ #

    def _one_pull(self, user: dict) -> str:
        """单次抽卡：根据概率更新用户数据，返回结果文案。"""
        r = random.randint(1, 100)
        if r <= LEGEND_RATE:
            user["传说"] += 1
            user["金币"] += LEGEND_GOLD
            return f"传说！！！，恭喜你抽到了最高品质！金币添加{LEGEND_GOLD}。"
        if r <= EPIC_RATE:
            user["史诗"] += 1
            user["金币"] += EPIC_GOLD
            return f"史诗！，恭喜你抽到了史诗，金币添加{EPIC_GOLD}。"
        if r <= RARE_RATE:
            user["高级"] += 1
            user["金币"] += RARE_GOLD
            return f"高级。也就一般般，金币添加{RARE_GOLD}。"
        user["空白"] += 1
        return "什么都没有。有点可惜喵。"

    # ------------------------------------------------------------------ #
    # 指令处理器                                                          #
    # ------------------------------------------------------------------ #

    @filter.command("查看版本", alias={"版本"})
    async def ver(self, event: AstrMessageEvent):
        """查看插件版本。"""
        yield event.plain_result(
            f"由莃棠制作\n当前版本为{PLUGIN_VERSION}\n"
            "新增记录抽奖次数，指令：查询记录、重置次数"
        )

    @filter.command("签到")
    async def sign_in(self, event: AstrMessageEvent):
        """每日签到，获得 1~100 金币与 1 点好感。"""
        uid = event.get_sender_id()
        if not uid:
            yield event.plain_result("无法获取你的身份信息喵～")
            return
        today = self._today(event)
        with self._lock:
            u = self._get_user(uid)
            if u["签到时限"] == today:
                reply = f"主人，您已经签到过了，你已签到了{u['签到天数']}天了喵"
            else:
                add_gold = random.randint(1, 100)
                u["签到天数"] += 1
                u["签到时限"] = today
                u["金币"] += add_gold
                u["好感"] += 1
                self._save_data()
                reply = (
                    f"{event.get_sender_name()} ，主人签到成功喵～，"
                    f"恭喜获得{add_gold}个金币，并且在之前已签到{u['签到天数'] - 1}天，"
                    "对你的好感又提升了喵呜～"
                )
        yield event.plain_result(reply)

    @filter.command("好感", alias={"好感度查询", "当前好感值"})
    async def query_like(self, event: AstrMessageEvent):
        """查询当前好感度。"""
        uid = event.get_sender_id()
        if not uid:
            yield event.plain_result("无法获取你的身份信息喵～")
            return
        with self._lock:
            u = self._get_user(uid)
            like = u["好感"]
        tip = random.choice(RANDOM_TIPS)
        yield event.plain_result(
            f"{event.get_sender_name()} 主人，\n当前我们的好感值为：{like}\nTip：\n{tip}"
        )

    @filter.command("金币", alias={"金币数量查询"})
    async def query_gold(self, event: AstrMessageEvent):
        """查询当前金币数量。"""
        uid = event.get_sender_id()
        if not uid:
            yield event.plain_result("无法获取你的身份信息喵～")
            return
        with self._lock:
            u = self._get_user(uid)
            gold = u["金币"]
        yield event.plain_result(f"主人，您一共有{gold}个金币，喵～")

    @filter.command("今日运势")
    async def fortune(self, event: AstrMessageEvent):
        """每日运势：品味值 / 运气值 / 厄运值 / 抽卡出货率。"""
        uid = event.get_sender_id()
        if not uid:
            yield event.plain_result("无法获取你的身份信息喵～")
            return
        today = self._today(event)
        month_day = datetime.strptime(today, "%Y-%m-%d").strftime("%m月%d日")
        with self._lock:
            u = self._get_user(uid)
            if u["运势日期"] == today:
                reply = (
                    f"{event.get_sender_name()} 主人，你今天已经测过了呀，您的今日运势为：\n"
                    f"品味值：{u['品味值']}；\n运气值：{u['运气值']}；\n"
                    f"厄运值：{u['厄运值']}；\n抽卡出货率：{u['出货率记录']}％。\n"
                    f"{month_day}\n喵～"
                )
            else:
                p = random.randint(1, 100)
                y = random.randint(1, 100)
                e = random.randint(1, 100)
                rate = random.randint(0, 100)
                u["运势日期"] = today
                u["品味值"] = p
                u["运气值"] = y
                u["厄运值"] = e
                u["出货率记录"] = rate
                self._save_data()
                reply = (
                    f"{event.get_sender_name()} ，主人，您的今日运势为：\n"
                    f"品味值：{p}；\n运气值：{y}；\n厄运值：{e}；\n抽卡出货率：{rate}％。\n"
                    f"{month_day}\n喵～"
                )
        yield event.plain_result(reply)

    @filter.command("抽奖", alias={"抽卡"})
    async def pull_one(self, event: AstrMessageEvent):
        """单抽一次，消耗 1 个抽奖币。"""
        uid = event.get_sender_id()
        if not uid:
            yield event.plain_result("无法获取你的身份信息喵～")
            return
        with self._lock:
            u = self._get_user(uid)
            if u["抽奖币"] < 1:
                reply = "主人，你没有抽奖币 喵～"
            else:
                u["抽奖币"] -= 1
                u["总抽数"] += 1
                res = self._one_pull(u)
                self._save_data()
                reply = (
                    f"当前抽奖结果为：{res} "
                    f"当前你还剩余{u['抽奖币']}个抽奖币，主人加油 喵～"
                )
        yield event.plain_result(reply)

    @filter.command("五连抽", alias={"五连"})
    async def pull5(self, event: AstrMessageEvent):
        """五连抽，消耗 5 个抽奖币。"""
        uid = event.get_sender_id()
        if not uid:
            yield event.plain_result("无法获取你的身份信息喵～")
            return
        with self._lock:
            u = self._get_user(uid)
            if u["抽奖币"] < 5:
                reply = f"{event.get_sender_name()}\n主人，你没有足够的抽奖币 喵～"
            else:
                out = []
                for i in range(5):
                    u["抽奖币"] -= 1
                    u["总抽数"] += 1
                    out.append(f"第{i + 1}发：{self._one_pull(u)}")
                self._save_data()
                reply = (
                    f"{event.get_sender_name()}\n你抽到了：\n"
                    + "\n".join(out)
                    + f"\n当前你还剩余{u['抽奖币']}个抽奖币"
                )
        yield event.plain_result(reply)

    @filter.command("十连抽", alias={"十连", "10连抽", "10连"})
    async def pull10(self, event: AstrMessageEvent):
        """十连抽，消耗 10 个抽奖币。"""
        uid = event.get_sender_id()
        if not uid:
            yield event.plain_result("无法获取你的身份信息喵～")
            return
        with self._lock:
            u = self._get_user(uid)
            if u["抽奖币"] < 10:
                reply = f"{event.get_sender_name()}\n主人，你没有足够的抽奖币 喵～"
            else:
                out = []
                for i in range(10):
                    u["抽奖币"] -= 1
                    u["总抽数"] += 1
                    out.append(f"第{i + 1}发：{self._one_pull(u)}")
                self._save_data()
                reply = (
                    f"{event.get_sender_name()}\n你抽到了：\n"
                    + "\n".join(out)
                    + f"\n当前你还剩余{u['抽奖币']}个抽奖币"
                )
        yield event.plain_result(reply)

    @filter.command("查询记录", alias={"抽卡记录", "抽奖记录"})
    async def query_record(self, event: AstrMessageEvent):
        """查询抽卡历史记录。"""
        uid = event.get_sender_id()
        if not uid:
            yield event.plain_result("无法获取你的身份信息喵～")
            return
        with self._lock:
            u = self._get_user(uid)
            total_gold = u["传说"] * LEGEND_GOLD + u["史诗"] * EPIC_GOLD + u["高级"] * RARE_GOLD
            reply = (
                f"{event.get_sender_name()}，你总共抽了{u['总抽数']}次\n"
                f"共计获得了{total_gold}枚金币\n"
                f"抽到过传说{u['传说']}次\n"
                f"抽到过史诗{u['史诗']}次\n"
                f"抽到过高级{u['高级']}次\n"
                f"抽到过无结果{u['空白']}次"
            )
        yield event.plain_result(reply)

    @filter.command("重置次数", alias={"重置记录"})
    async def reset_record(self, event: AstrMessageEvent):
        """重置抽卡记录（不影响金币与抽奖币）。"""
        uid = event.get_sender_id()
        if not uid:
            yield event.plain_result("无法获取你的身份信息喵～")
            return
        with self._lock:
            u = self._get_user(uid)
            u["传说"] = 0
            u["史诗"] = 0
            u["高级"] = 0
            u["空白"] = 0
            u["总抽数"] = 0
            self._save_data()
        yield event.plain_result("已重置所有次数，你可以查询记录查看是否重置完成喵～")

    @filter.command("金币商店", alias={"商店", "查看商品"})
    async def shop(self, event: AstrMessageEvent):
        """查看商店与商品价格。"""
        uid = event.get_sender_id()
        if not uid:
            yield event.plain_result("无法获取你的身份信息喵～")
            return
        with self._lock:
            u = self._get_user(uid)
            gold = u["金币"]
        txt = (
            "商店\n主人，在这里可以购买东西 喵～\n"
            f"购买格式：购买商品名 数量\n您当前金币为{gold}。\n"
            "①抽奖币：50金币；\n"
            "②糖果：10金币；（赠送后可以增加2点好感度）\n"
            "③袋装零食：25金币；（赠送后可以增加5点好感度）\n"
            "④鲜花：45金币；（赠送后可以增加10点好感度）\n"
            "⑤小说：60金币；（赠送后可以增加12点好感度）\n"
            "⑥蛋糕：120金币；（赠送后可以增加25点好感度）\n"
            "⑦暂无"
        )
        yield event.plain_result(txt)

    @filter.regex(r"^购买(.+?)\s*(\d+)$")
    async def buy_item(self, event: AstrMessageEvent):
        """购买商品，格式：购买商品名 数量（如：购买糖果 2）。"""
        m = re.match(r"^购买(.+?)\s*(\d+)$", event.get_message_str().strip())
        if not m:
            return
        goods = m.group(1).strip()
        num = int(m.group(2))
        uid = event.get_sender_id()
        if not uid:
            yield event.plain_result("无法获取你的身份信息喵～")
            return
        if goods not in PRICE_MAP:
            yield event.plain_result("没有该商品，请查看商店")
            return
        cost, key = PRICE_MAP[goods]
        total_cost = cost * num
        if num <= 0:
            yield event.plain_result("当前操作不合法")
            return
        with self._lock:
            u = self._get_user(uid)
            if u["金币"] < total_cost:
                reply = f"主人，你的金币不够，你当前只有{u['金币']}个金币 喵～"
            else:
                u["金币"] -= total_cost
                if key == "抽奖币":
                    u["抽奖币"] += num
                else:
                    u[key] += num
                remain = u["金币"]
                self._save_data()
                reply = f"主人，恭喜购买成功喵～\n你当前还有{remain}个金币"
        yield event.plain_result(reply)

    @filter.command("获得金币")
    async def daily_gold(self, event: AstrMessageEvent):
        """每日领取 200 金币。"""
        uid = event.get_sender_id()
        if not uid:
            yield event.plain_result("无法获取你的身份信息喵～")
            return
        today = self._today(event)
        with self._lock:
            u = self._get_user(uid)
            if u["每日金币领取"] == today:
                reply = "主人，您今天已经获得过了喵～"
            else:
                u["每日金币领取"] = today
                u["金币"] += 200
                self._save_data()
                reply = f"{event.get_sender_name()} 主人，恭喜成功获得200个金币喵～"
        yield event.plain_result(reply)

    @filter.command("领取抽奖币", alias={"获得抽奖币", "每日抽奖币"})
    async def free_ticket(self, event: AstrMessageEvent):
        """每日免费领取 1~10 个抽奖币。"""
        uid = event.get_sender_id()
        if not uid:
            yield event.plain_result("无法获取你的身份信息喵～")
            return
        today = self._today(event)
        with self._lock:
            u = self._get_user(uid)
            if u["每日免费抽币"] == today:
                reply = "主人，您今天已经获得过了喵～"
            else:
                add = random.randint(1, 10)
                u["每日免费抽币"] = today
                u["抽奖币"] += add
                self._save_data()
                reply = f"{event.get_sender_name()}已赠送每日抽奖币，获得抽奖币{add}喵"
        yield event.plain_result(reply)

    @filter.regex(r"^打钱\s*(\d+)$")
    async def master_give(self, event: AstrMessageEvent):
        """主人专属：给主人发金币，格式：打钱 数量。"""
        m = re.match(r"^打钱\s*(\d+)$", event.get_message_str().strip())
        if not m:
            return
        uid = event.get_sender_id()
        if not uid:
            yield event.plain_result("无法获取你的身份信息喵～")
            return
        if not self.master_qq:
            yield event.plain_result("主人还没有在插件配置中设置 master_qq 喵～")
            return
        if uid != self.master_qq:
            yield event.plain_result("你不是我真正的主人喵")
            return
        add = int(m.group(1))
        with self._lock:
            u = self._get_user(uid)
            u["金币"] += add
            self._save_data()
        yield event.plain_result(f"已经给主人{add}金币了，希望主人不要乱花钱喵")

    @filter.regex(r"^赠送$|^赠送礼物$|^赠送说明$")
    async def gift_help(self, event: AstrMessageEvent):
        """赠送礼物帮助，格式：赠送〔名称〕（空格）数量。"""
        yield event.plain_result(
            "主人，你要给我送什么礼物，喵？\n"
            "赠送格式：赠送〔名称〕（空格）数量\n"
            "①糖果\n②袋装零食\n③鲜花\n④小说\n⑤蛋糕"
        )

    @filter.regex(r"^赠送(.+?)\s*(\d+)$")
    async def send_gift(self, event: AstrMessageEvent):
        """赠送礼物提升好感，格式：赠送 礼物名 数量。"""
        m = re.match(r"^赠送(.+?)\s*(\d+)$", event.get_message_str().strip())
        if not m:
            return
        goods = m.group(1).strip()
        num = int(m.group(2))
        uid = event.get_sender_id()
        if not uid:
            yield event.plain_result("无法获取你的身份信息喵～")
            return
        if goods not in GIFT_MAP:
            yield event.plain_result("没有这个礼物")
            return
        g_key, add_like = GIFT_MAP[goods]
        if num <= 0:
            yield event.plain_result("主人，当前操作不合法喵")
            return
        with self._lock:
            u = self._get_user(uid)
            if u[g_key] < num:
                reply = random.choice(RANDOM_NO_ITEM)
            else:
                u[g_key] -= num
                u["好感"] += add_like * num
                left = u[g_key]
                thanks = random.choice(RANDOM_THANKS)
                self._save_data()
                reply = f"{thanks}\n（目前你还有{left}{goods}）"
        yield event.plain_result(reply)

    @filter.command("打卡")
    async def group_clock(self, event: AstrMessageEvent):
        """群打卡，返回当前群号。"""
        group_id = event.get_group_id()
        yield event.plain_result("群打卡：" + str(group_id))

    @filter.regex(r"^(叫|说)\s+(.+)$")
    async def speak_copy(self, event: AstrMessageEvent):
        """让机器人学说话（好感度达到 100 后可用），格式：说 内容。"""
        m = re.match(r"^(叫|说)\s+(.+)$", event.get_message_str().strip())
        if not m:
            return
        text = m.group(2)
        uid = event.get_sender_id()
        if not uid:
            yield event.plain_result("无法获取你的身份信息喵～")
            return
        with self._lock:
            u = self._get_user(uid)
            like = u["好感"]
        if like < 100:
            yield event.plain_result("我们当前的好感度不足以让我来学你说话")
            return
        yield event.plain_result(f"{text}～")

    @filter.regex(r"^群头衔\s+(.+)$")
    async def set_title(self, event: AstrMessageEvent):
        """设置群头衔（仅 QQ aiocqhttp 平台，机器人为群主/管理员时可用）。"""
        m = re.match(r"^群头衔\s+(.+)$", event.get_message_str().strip())
        if not m:
            return
        title = m.group(1).strip()
        if not title:
            yield event.plain_result("群头衔不能为空喵～")
            return
        if len(title) > 6:
            yield event.plain_result("群头衔最多不能超过六个字")
            return
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("当前不在群聊中，无法设置群头衔喵～")
            return
        # 群头衔依赖 QQ 协议端 API，仅 aiocqhttp 适配器支持
        if event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("当前平台不支持设置群头衔喵～")
            return
        bot = getattr(event, "bot", None)
        if bot is None or not hasattr(bot, "api"):
            yield event.plain_result("当前协议端不支持设置群头衔喵～")
            return
        try:
            await bot.api.call_action(
                "set_group_special_title",
                group_id=int(group_id) if group_id.isdigit() else group_id,
                user_id=event.get_sender_id(),
                special_title=title,
                duration=-1,
            )
            yield event.plain_result("设置成功了喵")
        except Exception as e:
            logger.warning(f"设置群头衔失败：{e}")
            yield event.plain_result(
                f"设置群头衔失败：{e}。请确认机器人为群主或管理员，且协议端支持该接口。"
            )

    @filter.command("早上好", alias={"早"})
    async def good_morning(self, event: AstrMessageEvent):
        """早安问候。"""
        yield event.plain_result(f"{event.get_sender_name()}，主人，早上好，喵～")

    @filter.command("晚安")
    async def good_night(self, event: AstrMessageEvent):
        """晚安问候。"""
        yield event.plain_result("哦！主人晚安，祝你好梦，喵～")

    @filter.command("晚上好", alias={"晚"})
    async def good_evening(self, event: AstrMessageEvent):
        """晚间问候。"""
        yield event.plain_result(
            f"{event.get_sender_name()}，主人酱，晚上好呀，祝你有一个好的夜晚，喵～"
        )

    async def terminate(self):
        """插件卸载/停用时保存当前数据。"""
        self._save_data()
