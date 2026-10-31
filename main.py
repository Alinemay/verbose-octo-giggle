import random
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


def get_json(url, headers=None, timeout=15, retries=2):
    """带超时和重试地请求接口并解析 JSON。

    返回 dict / list；任何环节失败（网络异常、非 200、返回内容不是 JSON）
    都返回 None，由调用方决定如何降级，不会再抛 JSONDecodeError。
    """
    for attempt in range(1, retries + 1):
        try:
            r = get(url, headers=headers, timeout=timeout)
        except Exception as e:
            print("请求失败({0}/{1}) {2} -> {3}".format(attempt, retries, url, e))
            continue
        if r.status_code != 200:
            print("接口返回非 200({0}/{1}) {2} -> HTTP {3}".format(attempt, retries, url, r.status_code))
            continue
        try:
            return r.json()
        except ValueError:
            # 这里就是原报错的位置：返回的是 HTML 错误页或空串，不是 JSON
            preview = r.text[:200].replace("\n", " ")
            print("接口返回内容不是合法 JSON({0}/{1}) {2} -> 状态码 {3}，内容类型 {4}，正文预览：{5}"
                  .format(attempt, retries, url, r.status_code,
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
    # 天气
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/103.0.0.0 Safari/537.36'
    }
    key = config["weather_key"]
    region_url = "https://geoapi.qweather.com/v2/city/lookup?location={}&key={}".format(region, key)
    response = get_json(region_url, headers=headers)
    if not isinstance(response, dict):
        print("获取地区信息失败，本次推送使用占位数据")
        return "未知", "--", "--", "--", "--", "--", "--", "", "", ""
    if response.get("code") == "404":
        print("推送消息失败，请检查地区名是否有误！")
        wait_if_windows()
        sys.exit(1)
    elif response.get("code") == "401":
        print("推送消息失败，请检查和风天气key是否正确！")
        wait_if_windows()
        sys.exit(1)
    else:
        # 获取地区的location--id
        location_id = response["location"][0]["id"]
    weather_url = "https://devapi.qweather.com/v7/weather/now?location={}&key={}".format(location_id, key)
    response = get_json(weather_url, headers=headers)
    if not isinstance(response, dict) or response.get("code") != "200":
        print("获取实时天气失败，本次推送使用占位数据")
        return "未知", "--", "--", "--", "--", "--", "--", "", "", ""
    # 天气
    weather = response["now"]["text"]
    # 当前温度
    temp = response["now"]["temp"] + u"\N{DEGREE SIGN}" + "C"
    # 风向
    wind_dir = response["now"]["windDir"]
    # 获取逐日天气预报
    url = "https://devapi.qweather.com/v7/weather/3d?location={}&key={}".format(location_id, key)
    response = get_json(url, headers=headers)
    if not isinstance(response, dict) or response.get("code") != "200":
        print("获取逐日预报失败，最高最低温和日出日落用占位数据")
        return weather, temp, "--", "--", wind_dir, "--", "--", "", "", ""
    # 最高气温
    max_temp = response["daily"][0]["tempMax"] + u"\N{DEGREE SIGN}" + "C"
    # 最低气温
    min_temp = response["daily"][0]["tempMin"] + u"\N{DEGREE SIGN}" + "C"
    # 日出时间
    sunrise = response["daily"][0]["sunrise"]
    # 日落时间
    sunset = response["daily"][0]["sunset"]
    url = "https://devapi.qweather.com/v7/air/now?location={}&key={}".format(location_id, key)
    response = get_json(url, headers=headers)
    if isinstance(response, dict) and response.get("code") == "200":
        # 空气质量
        category = response["now"]["category"]
        # pm2.5
        pm2p5 = response["now"]["pm2p5"]
    else:
        # 国外城市获取不到数据
        category = ""
        pm2p5 = ""
    id = random.randint(1, 16)
    url = "https://devapi.qweather.com/v7/indices/1d?location={}&key={}&type={}".format(location_id, key, id)
    response = get_json(url, headers=headers)
    proposal = ""
    if isinstance(response, dict) and response.get("code") == "200":
        proposal += response["daily"][0]["text"]
    return weather, temp, max_temp, min_temp, wind_dir, sunrise, sunset, category, pm2p5, proposal


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
    for url in ("https://open.iciba.com/dsapi/", "http://open.iciba.com/dsapi/"):
        data = get_json(url, headers)
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


def send_message(to_user, access_token, region_name, weather, temp, wind_dir, note_ch, note_en, max_temp, min_temp,
                 sunrise, sunset, category, pm2p5, proposal, chp):
    url = "https://api.weixin.qq.com/cgi-bin/message/template/send?access_token={}".format(access_token)
    week_list = ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"]
    year = localtime().tm_year
    month = localtime().tm_mon
    day = localtime().tm_mday
    today = datetime.date(datetime(year=year, month=month, day=day))
    week = week_list[today.isoweekday() % 7]
    # 获取在一起的日子的日期格式
    love_year = int(config["love_date"].split("-")[0])
    love_month = int(config["love_date"].split("-")[1])
    love_day = int(config["love_date"].split("-")[2])
    love_date = date(love_year, love_month, love_day)
    # 获取在一起的日期差
    love_days = str(today.__sub__(love_date)).split(" ")[0]
    # 获取所有生日数据
    birthdays = {}
    for k, v in config.items():
        if k[0:5] == "birth":
            birthdays[k] = v
    data = {
        "touser": to_user,
        "template_id": config["template_id"],
        "url": "http://weixin.qq.com/download",
        "topcolor": "#FF0000",
        "data": {
            "date": {
                "value": "{} {}".format(today, week),
                "color": get_color()
            },
            "region": {
                "value": region_name,
                "color": get_color()
            },
            "weather": {
                "value": weather,
                "color": get_color()
            },
            "temp": {
                "value": temp,
                "color": get_color()
            },
            "wind_dir": {
                "value": wind_dir,
                "color": get_color()
            },
            "love_day": {
                "value": love_days,
                "color": get_color()
            },
            "note_en": {
                "value": note_en,
                "color": get_color()
            },
            "note_ch": {
                "value": note_ch,
                "color": get_color()
            },
            "max_temp": {
                "value": max_temp,
                "color": get_color()
            },
            "min_temp": {
                "value": min_temp,
                "color": get_color()
            },
            "sunrise": {
                "value": sunrise,
                "color": get_color()
            },
            "sunset": {
                "value": sunset,
                "color": get_color()
            },
            "category": {
                "value": category,
                "color": get_color()
            },
            "pm2p5": {
                "value": pm2p5,
                "color": get_color()
            },
            "proposal": {
                "value": proposal,
                "color": get_color()
            },
            "chp": {
                "value": chp,
                "color": get_color()
            },

        }
    }
    for key, value in birthdays.items():
        # 获取距离下次生日的时间
        name = str(value.get("name", "")).strip()
        try:
            birth_day = get_birthday(value["birthday"], year, today)
        except Exception as e:
            print("生日 {} 计算失败，已跳过：{}".format(key, e))
            continue
        if birth_day == 0:
            birthday_data = "祝{}生日快乐".format(name)
        else:
            birthday_data = "距{}生日还有{}天".format(name, birth_day)
        # 将生日数据插入data
        data["data"][key] = {"value": birthday_data, "color": get_color()}

    # 统一按 20 字规范压缩，保证"发出去的"就是"能看到的"
    for k, v in data["data"].items():
        v["value"] = clip_field(v["value"])

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
    # 传入地区获取天气信息
    region = config["region"]
    weather, temp, max_temp, min_temp, wind_dir, sunrise, sunset, category, pm2p5, proposal = get_weather(region)
    note_ch = config.get("note_ch", "")
    note_en = config.get("note_en", "")
    if not note_ch or not note_en:
        # 获取词霸每日金句（任一为空即补全，失败会返回本地兜底金句）
        note_ch, note_en = get_ciba()
    chp = get_tianhang()
    # 公众号推送消息
    for user in users:
        send_message(user, accessToken, region, weather, temp, wind_dir, note_ch, note_en, max_temp, min_temp, sunrise,
                     sunset, category, pm2p5, proposal, chp)
    wait_if_windows()
