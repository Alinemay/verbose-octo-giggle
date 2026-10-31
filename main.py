import random
import re
from urllib.parse import quote
from time import localtime
from requests import get, post
from datetime import datetime, date
from zhdate import ZhDate
import sys
import os


# ---------------------------------------------------------------------------
# 通用工具：所有外部接口统一走这里，避免任何一个接口挂掉就把整个程序带崩
# ---------------------------------------------------------------------------
def wait_if_windows():
    """只有 Windows 本地运行时才暂停窗口；GitHub Actions 等 Linux 环境直接跳过"""
    if os.name == "nt":
        os.system("pause")


# ---------------------------------------------------------------------------
# 微信模板消息展示规范（2023-05-04 起生效）：
#   1) 去除自定义颜色、表情符号
#   2) 去除首行内容
#   3) 去除尾部/备注内容
#   4) 中间主内容中，单个字段不超过 20 字，且不支持换行
# 所以模板里排在最后的字段、以及超过 20 字的长句，都会被平台自动去掉。
# 下面统一按 20 字压缩，保证"发出去的内容"和"用户看到的内容"一致。
# ---------------------------------------------------------------------------
MAX_FIELD_LEN = 20


def clip_field(text, limit=MAX_FIELD_LEN):
    """按模板消息规范压缩字段取值：去掉换行和多余空格，超长部分截断。"""
    s = " ".join(str(text).split())
    if len(s) <= limit:
        return s
    return s[: max(limit - 1, 1)] + "…"


def mask_url(url):
    """日志里不能出现密钥：把 URL 查询串中的 key/secret 参数打码。"""
    return re.sub(r"((?:key|secret|appsecret|app_secret)=)[^&]+", r"\1***", str(url), flags=re.I)


def get_json(url, headers=None, timeout=15, retries=2):
    """带超时和重试地请求接口并解析 JSON。

    返回 dict / list；任何环节失败（网络异常、非 200、返回内容不是 JSON）
    都返回 None，由调用方决定如何降级，不会再抛 JSONDecodeError。
    """
    for attempt in range(1, retries + 1):
        try:
            r = get(url, headers=headers, timeout=timeout)
        except Exception as e:
            print("请求失败({0}/{1}) {2} -> {3}".format(attempt, retries, mask_url(url), e))
            continue
        if r.status_code != 200:
            print("接口返回非 200({0}/{1}) {2} -> HTTP {3}".format(attempt, retries, mask_url(url), r.status_code))
            continue
        try:
            return r.json()
        except ValueError:
            # 这里就是原报错的位置：返回的是 HTML 错误页或空串，不是 JSON
            preview = r.text[:200].replace("\n", " ")
            print("接口返回内容不是合法 JSON({0}/{1}) {2} -> 状态码 {3}，内容类型 {4}，正文预览：{5}"
                  .format(attempt, retries, mask_url(url), r.status_code,
                          r.headers.get("Content-Type"), preview))
    return None


def get_color():
    # 获取随机颜色
    get_colors = lambda n: list(map(lambda i: "#" + "%06x" % random.randint(0, 0xFFFFFF), range(n)))
    color_list = get_colors(100)
    return random.choice(color_list)


def get_access_token():
    # appId
    app_id = config["app_id"]
    # appSecret
    app_secret = config["app_secret"]
    post_url = ("https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={}&secret={}"
                .format(app_id, app_secret))
    data = get_json(post_url, retries=3)
    if not isinstance(data, dict) or "access_token" not in data:
        print("获取access_token失败，请检查app_id和app_secret是否正确；接口返回：{}".format(data))
        wait_if_windows()
        sys.exit(1)
    access_token = data["access_token"]
    # print(access_token)
    return access_token


# 每个城市块的字段后缀，与微信模板里的 {{cityN_xxx.DATA}} 一一对应
CITY_SUFFIXES = ("feels", "hum", "min", "max", "uv", "rain", "rise", "set",
                 "air", "aqi", "moon")


def lookup_city(region, headers, key):
    """按地区名查城市，返回 (location_id, 城市短名)；查不到返回 (None, None)。"""
    url = "https://geoapi.qweather.com/v2/city/lookup?location={}&key={}".format(region, key)
    response = get_json(url, headers=headers)
    if not isinstance(response, dict):
        print("获取地区信息失败：{}".format(region))
        return None, None
    if response.get("code") == "401":
        print("推送消息失败，请检查和风天气key是否正确！")
        wait_if_windows()
        sys.exit(1)
    if response.get("code") != "200":
        print("地区名可能有误，找不到：{}（code={}）".format(region, response.get("code")))
        return None, None
    try:
        loc = response["location"][0]
        return loc["id"], loc.get("name") or region
    except (KeyError, IndexError, TypeError):
        print("地区信息解析失败：{}".format(region))
        return None, None


def fetch_region(location_id, region, name, headers, key, deg, full):
    """取一个地区的数据。

    每个地区都会拿到六行同构内容（城市+天气+气温、体感+湿度、温度区间、
    空气+AQI、紫外线+降水、日出+日落）；full=True 时再补生活指数、
    灾害预警、分钟降水这类整条推送只需要一份的数据。
    """
    w = {
        "region": region, "city_name": name,
        "weather": "未知", "temp": "--", "feels": "--", "humidity": "--",
        "max_temp": "--", "min_temp": "--", "sunrise": "--", "sunset": "--",
        "uv": "--", "precip": "--", "tomorrow": "--", "moon": "--",
        "category": "", "aqi": "", "proposal": "", "warning": "", "rain_tip": "",
        "line": name,
    }
    # 实时天气：天气现象、气温、体感温度、湿度
    url = "https://devapi.qweather.com/v7/weather/now?location={}&key={}".format(location_id, key)
    response = get_json(url, headers=headers)
    if isinstance(response, dict) and response.get("code") == "200":
        now = response["now"]
        w["weather"] = now.get("text", "未知")
        w["temp"] = str(now.get("temp", "--")) + deg
        w["feels"] = str(now.get("feelsLike", "--")) + deg
        w["humidity"] = str(now.get("humidity", "--")) + "%"
        # 城市行用 config 里配置的完整地区名，而不是和风返回的短名（如“绍兴”）；
        # 遇到“雷阵雨伴有冰雹”这类长天气词导致超过 20 字时，退回短名以免被截断
        line = "{} {} {}".format(region, w["weather"], w["temp"])
        if len(line) > MAX_FIELD_LEN:
            line = "{} {} {}".format(name, w["weather"], w["temp"])
        w["line"] = line
    else:
        print("获取实时天气失败：{}".format(region))

    # 逐日预报：温度区间、日出日落、紫外线、降水量、月相、明日天气
    url = "https://devapi.qweather.com/v7/weather/3d?location={}&key={}".format(location_id, key)
    response = get_json(url, headers=headers)
    if isinstance(response, dict) and response.get("code") == "200" and response.get("daily"):
        daily = response["daily"]
        today_fc = daily[0]
        w["max_temp"] = str(today_fc.get("tempMax", "--")) + deg
        w["min_temp"] = str(today_fc.get("tempMin", "--")) + deg
        w["sunrise"] = today_fc.get("sunrise", "--")
        w["sunset"] = today_fc.get("sunset", "--")
        w["uv"] = str(today_fc.get("uvIndex", "--"))
        # 注意：v7 的 daily 里给的是降水量 precip（毫米），没有降水概率字段
        w["precip"] = str(today_fc.get("precip", "--")) + "mm"
        w["moon"] = today_fc.get("moonPhase", "--")
        if len(daily) > 1:
            d1 = daily[1]
            w["tomorrow"] = "{} {}~{}".format(d1.get("textDay", ""), d1.get("tempMin", ""),
                                              d1.get("tempMax", "")) + deg
    else:
        print("获取逐日预报失败：{}".format(region))

    # 空气质量：等级与 AQI，每个地区都要一份
    url = "https://devapi.qweather.com/v7/air/now?location={}&key={}".format(location_id, key)
    response = get_json(url, headers=headers)
    if isinstance(response, dict) and response.get("code") == "200":
        w["category"] = response["now"].get("category", "")
        w["aqi"] = str(response["now"].get("aqi", ""))
    else:
        # 国外城市获取不到数据
        print("获取空气质量失败，该字段留空：{}".format(region))

    if not full:
        return w

    # 以下三项整条推送只需要一份，只对主地区取，避免请求数按城市数量翻倍
    # 生活指数：随机取一类，只保留建议文字
    index_type = random.randint(1, 16)
    url = "https://devapi.qweather.com/v7/indices/1d?location={}&key={}&type={}".format(
        location_id, key, index_type)
    response = get_json(url, headers=headers)
    if isinstance(response, dict) and response.get("code") == "200" and response.get("daily"):
        w["proposal"] = response["daily"][0].get("text", "")

    # 灾害预警与分钟级降水
    w["warning"] = get_warning(location_id, key, headers)
    w["rain_tip"] = get_rain_tip(location_id, key, headers)
    return w


def get_weather(regions):
    """按地区列表取数据，每个地区输出结构完全相同的六行。"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36'
    }
    key = config["weather_key"]
    deg = u"\N{DEGREE SIGN}" + "C"
    w = {
        "region": regions[0] if regions else "", "weather": "未知", "temp": "--",
        "feels": "--", "humidity": "--", "max_temp": "--", "min_temp": "--",
        "sunrise": "--", "sunset": "--", "uv": "--", "precip": "--",
        "tomorrow": "--", "moon": "--", "category": "", "aqi": "",
        "proposal": "", "warning": "", "rain_tip": "", "lunar": "",
    }
    for i in range(1, 5):
        w["city{}".format(i)] = ""
        for suffix in CITY_SUFFIXES:
            w["city{}_{}".format(i, suffix)] = ""

    briefs = []
    detail_done = False
    for region in regions:
        location_id, name = lookup_city(region, headers, key)
        if not location_id:
            continue
        data = fetch_region(location_id, region, name, headers, key, deg, full=not detail_done)
        briefs.append(data)
        if not detail_done:
            # 第一个成功查询的地区作为主地区，提供明日天气、建议、预警等全局内容
            w.update(data)
            detail_done = True

    if not detail_done:
        print("所有地区均获取失败，本次推送使用占位数据")
    for i, data in enumerate(briefs[:4]):
        n = i + 1
        w["city{}".format(n)] = data["line"]
        w["city{}_feels".format(n)] = data["feels"]
        w["city{}_hum".format(n)] = data["humidity"]
        w["city{}_min".format(n)] = data["min_temp"]
        w["city{}_max".format(n)] = data["max_temp"]
        w["city{}_uv".format(n)] = data["uv"]
        w["city{}_rain".format(n)] = data["precip"]
        w["city{}_rise".format(n)] = data["sunrise"]
        w["city{}_set".format(n)] = data["sunset"]
        w["city{}_air".format(n)] = data["category"]
        w["city{}_aqi".format(n)] = data["aqi"]
        w["city{}_moon".format(n)] = data["moon"]

    w["lunar"] = get_lunar(date.today())
    return w


def get_warning(location_id, key, headers):
    """取当前生效的灾害预警标题，多条时标注总数。无预警返回空串。"""
    url = "https://devapi.qweather.com/v7/warning/now?location={}&key={}".format(location_id, key)
    response = get_json(url, headers=headers)
    if not isinstance(response, dict) or response.get("code") != "200":
        print("获取灾害预警失败（可能不在套餐内），该字段留空")
        return ""
    warnings = response.get("warning") or []
    if not warnings:
        return ""
    title = str(warnings[0].get("title", "")).strip()
    if len(warnings) > 1:
        title = "{}等{}条".format(title, len(warnings))
    return title


def get_rain_tip(location_id, key, headers):
    """取分钟级降水的一句话总结，接口不可用时返回空串。"""
    url = "https://devapi.qweather.com/v7/minutely/5m?location={}&key={}".format(location_id, key)
    response = get_json(url, headers=headers)
    if not isinstance(response, dict) or response.get("code") != "200":
        print("获取分钟级降水失败（可能不在套餐内），该字段留空")
        return ""
    return str(response.get("summary", "")).strip()


def get_lunar(today):
    """本地换算农历日期，例如“农历九月十一”。失败返回空串。"""
    try:
        if hasattr(ZhDate, "from_datetime"):
            lunar = ZhDate.from_datetime(datetime(today.year, today.month, today.day))
        else:
            lunar = ZhDate.today()
        text = lunar.chinese() if hasattr(lunar, "chinese") else str(lunar)
        # zhdate 各版本 chinese() 的返回格式不一致，可能带括号或“农历”前缀，
        # 这里只保留汉字再做裁剪，避免出现“农历)”这种残值
        text = re.sub(r"[^\u4e00-\u9fa5]", "", text)
        if text.startswith("农历"):
            text = text[2:]
        if "星期" in text:
            text = text.split("星期")[0]
        if "年" in text:
            text = text.split("年", 1)[1]
        if not text:
            return ""
        return "农历" + text
    except Exception as e:
        print("农历换算失败，该字段留空：{}".format(e))
        return ""


# 星座运势：接口返回 result.list，每项是“类型 + 内容”，这里把类型映射成字段后缀。
# 支持最多两个星座，字段名分别是 star1_all / star2_all 这种形式。
STAR_FIELDS = {
    "综合指数": "all",
    "爱情指数": "love",
    "工作指数": "work",
    "财运指数": "money",
    "健康指数": "health",
    "幸运颜色": "color",
    "幸运数字": "num",
    "贵人星座": "friend",
    "今日概述": "summary",
}


def get_star():
    """取星座运势，最多两个。config 里没填或接口失败时，所有字段留空。"""
    astros = config.get("star", [])
    if isinstance(astros, str):
        astros = [astros]
    astros = [str(a).strip() for a in astros if str(a).strip()][:2]

    out = {}
    for i in (1, 2):
        out["star{}_name".format(i)] = ""
        for suffix in STAR_FIELDS.values():
            out["star{}_{}".format(i, suffix)] = ""
    if not astros:
        return out
    for i, astro in enumerate(astros, 1):
        out["star{}_name".format(i)] = astro

    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36'
    }
    key = config.get("tian_api", "")
    for i, astro in enumerate(astros, 1):
        # astro 支持中文或英文名（如 狮子座 / leo），中文需 urlencode
        url = "https://apis.tianapi.com/star/index?key={}&astro={}".format(key, quote(astro))
        data = get_json(url, headers, timeout=10, retries=1)
        if not isinstance(data, dict) or data.get("code") != 200:
            print("星座运势接口不可用（未申请时返回 160），{} 留空：{}".format(astro, data))
            continue
        for item in (data.get("result") or {}).get("list") or []:
            suffix = STAR_FIELDS.get(item.get("type"))
            if suffix:
                out["star{}_{}".format(i, suffix)] = str(item.get("content", "")).strip()
    return out


def get_morning():
    """取早安心语（天行数据 zaoan 接口）。失败返回空串，由本地短句兜底。"""
    try:
        key = config["tian_api"]
        url = "https://apis.tianapi.com/zaoan/index?key={}".format(key)
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36',
            'Content-type': 'application/x-www-form-urlencoded'
        }
        response = get_json(url, headers, timeout=10, retries=1)
        if isinstance(response, dict) and response.get("code") == 200:
            return str((response.get("result") or {}).get("content", "")).strip()
        print("早安心语接口不可用（未申请时返回 160）：{}".format(response))
    except KeyError:
        pass
    return ""


def get_birthday(birthday, year, today):
    birthday_year = birthday.split("-")[0]
    # 判断是否为农历生日
    if birthday_year[0] == "r":
        r_mouth = int(birthday.split("-")[1])
        r_day = int(birthday.split("-")[2])
        # 获取农历生日的生日
        try:
            year_date = ZhDate(year, r_mouth, r_day).to_datetime().date()
        except TypeError:
            print("请检查生日的日子是否在今年存在")
            wait_if_windows()
            sys.exit(1)

    else:
        # 获取国历生日的今年对应月和日
        birthday_month = int(birthday.split("-")[1])
        birthday_day = int(birthday.split("-")[2])
        # 今年生日
        year_date = date(year, birthday_month, birthday_day)
    # 计算生日年份，如果还没过，按当年减，如果过了需要+1
    if today > year_date:
        if birthday_year[0] == "r":
            # 获取农历明年生日的月和日
            r_last_birthday = ZhDate((year + 1), r_mouth, r_day).to_datetime().date()
            birth_date = date((year + 1), r_last_birthday.month, r_last_birthday.day)
        else:
            birth_date = date((year + 1), birthday_month, birthday_day)
        birth_day = str(birth_date.__sub__(today)).split(" ")[0]
    elif today == year_date:
        birth_day = 0
    else:
        birth_date = year_date
        birth_day = str(birth_date.__sub__(today)).split(" ")[0]
    return birth_day


# 本地金句库：不依赖任何网络，按日期轮换，中英文都控制在 20 字以内。
# 天行数据接口不可用、或返回句子过长时，用它兜底，保证推送永远有内容。
SHORT_NOTES = [
    ("慢慢来，比较快", "Slow is fast"),
    ("行动胜过空想", "Act now"),
    ("今天也要好好吃饭", "Eat well today"),
    ("心安即是归处", "Peace is home"),
    ("热爱可抵岁月长", "Love outlasts time"),
    ("把日子过成诗", "Make life a poem"),
    ("早起的风最清醒", "Morning air is clear"),
    ("一点一点靠近想要的生活", "Step by step"),
    ("今天的太阳很温柔", "Gentle sun today"),
    ("认真生活的人最可爱", "Live with care"),
]

# 早安心语接口不可用时的兜底句子，都在 20 字以内
SHORT_MORNING = [
    "早安，今天也要好好吃饭",
    "新的一天，从认真吃早饭开始",
    "早安，愿你今天顺顺利利",
    "早安，阳光正在来的路上",
]


def clip_sentence(text, limit=MAX_FIELD_LEN):
    """超长时退到最后一个标点处截断，保证留下的是完整的一句话或短语。"""
    s = " ".join(str(text).split())
    if len(s) <= limit:
        return s
    head = s[:limit]
    for sep in ("。", "！", "？", "；", "，", "、", " "):
        idx = head.rfind(sep)
        if idx >= 6:  # 至少保留 6 个字，太短就不在这里断
            return head[:idx]
    return head[:limit]


def clip_morning(text, limit=MAX_FIELD_LEN):
    """早安心语的重点在结尾（“早安”），超长时从后往前按标点截，保住收尾。"""
    s = " ".join(str(text).split())
    if len(s) <= limit:
        return s
    segments = re.split(r"(?<=[，。！？；])", s)
    tail = ""
    for seg in reversed(segments):
        if len(tail) + len(seg) > limit:
            break
        tail = seg + tail
    tail = tail.strip()
    head = clip_sentence(s)
    # 保尾至少要有 10 个字才划算，否则还不如从前面多留点内容
    if len(tail) >= 10:
        return tail
    return head


def get_quote():
    """取每日金句。

    优先级：天行数据「每日英语」→ 本地短句库按日期轮换。
    原词霸接口 open.iciba.com 在 GitHub 运行器上长期连接超时，已移除。
    """
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36'
    }
    key = config.get("tian_api", "")
    if key:
        url = "https://apis.tianapi.com/everyday/index?key={}".format(key)
        data = get_json(url, headers, timeout=10, retries=1)
        if isinstance(data, dict) and data.get("code") == 200:
            result = data.get("result") or {}
            note_ch = str(result.get("note") or "").strip()
            note_en = str(result.get("content") or "").strip()
            if note_ch and len(note_ch) <= MAX_FIELD_LEN:
                # 英文原句通常几十上百字符，模板里放不下，超长就留空，避免残句
                if len(note_en) > MAX_FIELD_LEN:
                    note_en = ""
                return note_ch, note_en
            print("天行返回的金句为空或超过 20 字，改用本地短句：{}".format(note_ch))
        else:
            print("天行每日英语接口不可用（也未申请时返回 160），改用本地短句：{}".format(data))
    index = date.today().toordinal() % len(SHORT_NOTES)
    note_ch, note_en = SHORT_NOTES[index]
    print("本次使用本地短句第 {} 条：{} / {}".format(index + 1, note_ch, note_en))
    return note_ch, note_en


def send_message(to_user, access_token, w):
    url = "https://api.weixin.qq.com/cgi-bin/message/template/send?access_token={}".format(access_token)
    week_list = ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"]
    today = date.today()
    week = week_list[today.isoweekday() % 7]
    # 获取在一起的日子的日期格式
    love_year = int(config["love_date"].split("-")[0])
    love_month = int(config["love_date"].split("-")[1])
    love_day = int(config["love_date"].split("-")[2])
    love_date = date(love_year, love_month, love_day)
    # 获取在一起的日期差
    love_days = str(today.__sub__(love_date)).split(" ")[0]

    # 先按“字段名 → 文案”组织，最后统一包装成微信要求的 data 结构
    fields = {
        "date": "{} {}".format(today, week),
        "lunar": w.get("lunar", ""),
        "warning": w.get("warning", ""),
        "note_ch": w.get("note_ch", ""),
        "note_en": w.get("note_en", ""),
        "love_day": love_days,
        "tomorrow": w.get("tomorrow", ""),
        "rain_tip": w.get("rain_tip", ""),
        "proposal": w.get("proposal", ""),
        "chp": w.get("chp", ""),
        "morning": w.get("morning", ""),
        "star1_name": w.get("star1_name", ""),
        "star1_all": w.get("star1_all", ""),
        "star1_love": w.get("star1_love", ""),
        "star1_work": w.get("star1_work", ""),
        "star1_money": w.get("star1_money", ""),
        "star1_health": w.get("star1_health", ""),
        "star1_color": w.get("star1_color", ""),
        "star1_num": w.get("star1_num", ""),
        "star1_friend": w.get("star1_friend", ""),
        "star1_summary": w.get("star1_summary", ""),
        "star2_name": w.get("star2_name", ""),
        "star2_all": w.get("star2_all", ""),
        "star2_love": w.get("star2_love", ""),
        "star2_work": w.get("star2_work", ""),
        "star2_money": w.get("star2_money", ""),
        "star2_health": w.get("star2_health", ""),
        "star2_color": w.get("star2_color", ""),
        "star2_num": w.get("star2_num", ""),
        "star2_friend": w.get("star2_friend", ""),
        "star2_summary": w.get("star2_summary", ""),
    }

    # 每个城市：city1、city1_feels、city1_hum ……
    for n in range(1, 5):
        fields["city{}".format(n)] = w.get("city{}".format(n), "")
        for suffix in CITY_SUFFIXES:
            key = "city{}_{}".format(n, suffix)
            fields[key] = w.get(key, "")

    birth_keys = sorted(k for k in config if k.startswith("birth"))
    for i, key in enumerate(birth_keys[:2]):
        value = config[key]
        name = str(value.get("name", "")).strip()
        try:
            birth_day = get_birthday(value["birthday"], today.year, today)
        except Exception as e:
            print("生日 {} 计算失败，已跳过：{}".format(key, e))
            continue
        if birth_day == 0:
            fields["birthday{}".format(i + 1)] = "祝{}生日快乐".format(name)
        else:
            fields["birthday{}".format(i + 1)] = "距{}生日还有{}天".format(name, birth_day)

    # 统一按 20 字规范压缩，保证“发出去的”就是“能看到的”
    data = {
        "touser": to_user,
        "template_id": config["template_id"],
        "url": "http://weixin.qq.com/download",
        "topcolor": "#FF0000",
        "data": {k: {"value": clip_field(v), "color": get_color()} for k, v in fields.items()},
    }

    # 发送前自检：打印每个字段的名称、字数与内容，方便比对公众号后台的模板占位符
    print("---- 本次发送的模板字段（字段名 | 字数 | 内容）----")
    for k, v in data["data"].items():
        print("{} | {}字 | {}".format(k, len(v["value"]), v["value"]))
    print("提示：若上面字段都有值、微信却不显示，请到公众号后台核对占位符名称是否完全一致，"
          "并检查该字段是不是排在模板最后（尾部内容会被平台自动去除）。")

    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36'
    }
    try:
        resp = post(url, headers=headers, json=data, timeout=20)
        response = resp.json()
    except ValueError:
        print("推送消息失败，微信接口返回的不是 JSON，状态码 {}，正文预览：{}"
              .format(resp.status_code, resp.text[:200]))
        return
    # 原样打印微信返回，出问题时这行日志就是唯一证据
    print("微信接口返回：{}".format(response))
    errcode = response.get("errcode")
    if errcode == 40037:
        print("推送消息失败，请检查模板id是否正确")
    elif errcode == 40036:
        print("推送消息失败，请检查模板id是否为空")
    elif errcode == 40003:
        print("推送消息失败，请检查微信号是否正确")
    elif errcode == 47003:
        print("推送消息失败，参数值不合法（47003）：请核对模板字段名与取值格式")
    elif errcode == 0:
        print("推送消息成功")
    else:
        print("推送消息失败，错误码：{}".format(errcode))


if __name__ == "__main__":
    try:
        with open("config.txt", encoding="utf-8") as f:
            config = eval(f.read())
    except FileNotFoundError:
        print("推送消息失败，请检查config.txt文件是否与程序位于同一路径")
        wait_if_windows()
        sys.exit(1)
    except SyntaxError:
        print("推送消息失败，请检查配置文件格式是否正确")
        wait_if_windows()
        sys.exit(1)

    # 获取accessToken
    accessToken = get_access_token()
    # 接收的用户
    users = config["user"]
    # 地区支持单个字符串或多个地区的列表
    regions = config["region"]
    if isinstance(regions, str):
        regions = [regions]
    # 一次性拿到天气、空气、预警、指数、农历等全部展示数据
    w = get_weather(regions)
    note_ch = config.get("note_ch", "")
    note_en = config.get("note_en", "")
    if not note_ch or not note_en:
        # 取每日金句（天行优先，失败用本地短句）
        note_ch, note_en = get_quote()
    w["note_ch"] = note_ch
    w["note_en"] = note_en
    # 早安心语只取一次，保证两个收件人看到的内容一致；
    # 原句普遍超过 20 字，这里按标点截断，宁可短一点也不留半句话
    morning = get_morning()
    if not morning:
        morning = random.choice(SHORT_MORNING)
    w["chp"] = clip_morning(morning)
    w["morning"] = w["chp"]
    w.update(get_star())
    # 公众号推送消息
    for user in users:
        send_message(user, accessToken, w)
    wait_if_windows()
