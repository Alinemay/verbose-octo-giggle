import random
import re
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


def get_weather(region):
    """获取天气、空气、预警等全部展示数据，返回字典。

    任何一项接口失败都用占位值填充，保证推送照常发出。
    """
    # 天气
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36'
    }
    key = config["weather_key"]
    deg = u"\N{DEGREE SIGN}" + "C"
    w = {
        "region": region, "weather": "未知", "temp": "--", "feels": "--",
        "humidity": "--",
        "max_temp": "--", "min_temp": "--", "sunrise": "--", "sunset": "--",
        "uv": "--", "precip": "--", "tomorrow": "--", "moon": "--",
        "category": "", "aqi": "",
        "proposal": "", "warning": "", "rain_tip": "",
        "lunar": "",
    }
    region_url = "https://geoapi.qweather.com/v2/city/lookup?location={}&key={}".format(region, key)
    response = get_json(region_url, headers=headers)
    if not isinstance(response, dict):
        print("获取地区信息失败，本次推送使用占位数据")
        return w
    if response.get("code") == "404":
        print("推送消息失败，请检查地区名是否有误！")
        wait_if_windows()
        sys.exit(1)
    elif response.get("code") == "401":
        print("推送消息失败，请检查和风天气key是否正确！")
        wait_if_windows()
        sys.exit(1)
    # 获取地区的location--id
    location_id = response["location"][0]["id"]

    # 实时天气：天气现象、气温、体感温度、湿度、能见度、风向
    url = "https://devapi.qweather.com/v7/weather/now?location={}&key={}".format(location_id, key)
    response = get_json(url, headers=headers)
    if isinstance(response, dict) and response.get("code") == "200":
        now = response["now"]
        w["weather"] = now.get("text", "未知")
        w["temp"] = str(now.get("temp", "--")) + deg
        w["feels"] = str(now.get("feelsLike", "--")) + deg
        w["humidity"] = str(now.get("humidity", "--")) + "%"
    else:
        print("获取实时天气失败，使用占位数据")

    # 逐日预报：最高最低温、日出日落、紫外线、降水量、月相、明日天气
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
        print("获取逐日预报失败，使用占位数据")

    # 空气质量：等级、pm2.5、AQI
    url = "https://devapi.qweather.com/v7/air/now?location={}&key={}".format(location_id, key)
    response = get_json(url, headers=headers)
    if isinstance(response, dict) and response.get("code") == "200":
        w["category"] = response["now"].get("category", "")
        w["aqi"] = str(response["now"].get("aqi", ""))
    else:
        # 国外城市获取不到数据
        print("获取空气质量失败，该字段留空")

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

    # 农历与周末，纯本地计算，不依赖网络
    today = date.today()
    w["lunar"] = get_lunar(today)
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
        if hasattr(lunar, "chinese"):
            return "农历" + lunar.chinese().split("年")[-1]
        return "农历" + str(lunar)
    except Exception as e:
        print("农历换算失败，该字段留空：{}".format(e))
        return ""


def get_tianhang():
    try:
        key = config["tian_api"]
        url = "http://api.tianapi.com/caihongpi/index?key={}".format(key)
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36',
            'Content-type': 'application/x-www-form-urlencoded'

        }
        response = get_json(url, headers)
        if isinstance(response, dict) and response.get("code") == 200:
            chp = response["newslist"][0]["content"]
        else:
            chp = ""
    except (KeyError, IndexError, TypeError):
        chp = ""
    return chp


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


# 短句金句库：每条中英文都控制在 20 字以内，符合微信模板消息的展示规范。
# 既作为接口失败时的兜底，也用于替换过长的每日一句（长句在微信里会被截成残句）。
SHORT_NOTES = [
    ("慢慢来，比较快", "Slow is fast"),
    ("行动胜过空想", "Act now"),
    ("今天也要好好吃饭", "Eat well today"),
    ("心安即是归处", "Peace is home"),
    ("热爱可抵岁月长", "Love outlasts time"),
]

# 彩虹屁原句通常几十字，直接截断会变成半句话，超长时改用这些短句
SHORT_CHP = [
    "今天的你闪闪发光",
    "元气满满的一天",
    "你笑起来真好看",
    "今天也是被你可爱到的一天",
]


def clip_or_pick(text, pool, limit=MAX_FIELD_LEN):
    """不超过 limit 就原样返回，超长则换一条同类短句，避免出现截断后的半句话。"""
    s = " ".join(str(text).split())
    if len(s) <= limit:
        return s
    return random.choice(pool)


def get_ciba():
    """获取词霸每日一句。

    原代码直接 r.json()["content"]，接口一旦返回 HTML 错误页或空内容就会抛
    JSONDecodeError 导致整个 GitHub Actions 任务失败。这里改为：
    先判断状态码，再安全解析，过长或失败时用本地短句兜底。
    """
    headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36'
    }
    # 优先 https，失败再退回 http（http 会 301 跳转到 https）
    # 该域名在 GitHub 运行器上经常直接超时，所以缩短超时并只试一次，
    # 避免为了一个可有可无的金句白等一分钟
    for url in ("https://open.iciba.com/dsapi/", "http://open.iciba.com/dsapi/"):
        data = get_json(url, headers, timeout=8, retries=1)
        if not isinstance(data, dict):
            continue
        note_en = str(data.get("content") or "").strip()
        note_ch = str(data.get("note") or "").strip()
        if not (note_en and note_ch):
            print("词霸接口返回的数据缺少 content/note 字段：{}".format(data))
            continue
        # 微信单个字段上限 20 字，长句会被截断成半句，因此过长的句子改用本地短句
        if len(note_ch) > MAX_FIELD_LEN or len(note_en) > MAX_FIELD_LEN:
            print("词霸金句超出 20 字（中文 {} 字，英文 {} 字符），微信会截断，已改用本地短句。"
                  "原始内容：{} / {}".format(len(note_ch), len(note_en), note_ch, note_en))
            break
        return note_ch, note_en
    note_ch, note_en = random.choice(SHORT_NOTES)
    print("本次使用本地短句：{} / {}".format(note_ch, note_en))
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
        "note_ch": w.get("note_ch", ""),
        "note_en": w.get("note_en", ""),
        "region": w.get("region", ""),
        "weather": w.get("weather", ""),
        "temp": w.get("temp", ""),
        "feels": w.get("feels", ""),
        "min_temp": w.get("min_temp", ""),
        "max_temp": w.get("max_temp", ""),
        "humidity": w.get("humidity", ""),
        "uv": w.get("uv", ""),
        "precip": w.get("precip", ""),
        "category": w.get("category", ""),
        "aqi": w.get("aqi", ""),
        "sunrise": w.get("sunrise", ""),
        "sunset": w.get("sunset", ""),
        "moon": w.get("moon", ""),
        "tomorrow": w.get("tomorrow", ""),
        "rain_tip": w.get("rain_tip", ""),
        "warning": w.get("warning", ""),
        "proposal": w.get("proposal", ""),
        "chp": w.get("chp", ""),
        "love_day": love_days,
    }

    # 生日字段按 config 里的 birthday* 动态插入
    for key, value in config.items():
        if key[0:5] != "birth":
            continue
        name = str(value.get("name", "")).strip()
        try:
            birth_day = get_birthday(value["birthday"], today.year, today)
        except Exception as e:
            print("生日 {} 计算失败，已跳过：{}".format(key, e))
            continue
        if birth_day == 0:
            fields[key] = "祝{}生日快乐".format(name)
        else:
            fields[key] = "距{}生日还有{}天".format(name, birth_day)

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
    # 一次性拿到天气、空气、预警、指数、农历等全部展示数据
    w = get_weather(config["region"])
    note_ch = config.get("note_ch", "")
    note_en = config.get("note_en", "")
    if not note_ch or not note_en:
        # 获取词霸每日金句（任一为空即补全，失败会返回本地兜底金句）
        note_ch, note_en = get_ciba()
    w["note_ch"] = note_ch
    w["note_en"] = note_en
    # 彩虹屁只取一次，保证两个收件人看到的内容一致
    w["chp"] = clip_or_pick(get_tianhang(), SHORT_CHP)
    # 公众号推送消息
    for user in users:
        send_message(user, accessToken, w)
    wait_if_windows()
