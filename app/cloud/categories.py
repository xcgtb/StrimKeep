"""Classification decisions keep TMDB movie/tv identity separate from folders."""

from app.cloud.errors import CloudError
from app.cloud.naming import clean

DEFAULT = ["电影", "剧集", "动漫", "纪录片", "综艺"]
OPTIONAL = ["儿童节目", "演唱会"]
COUNTRIES = {"CN":"中国大陆", "HK":"香港", "TW":"台湾", "JP":"日本", "KR":"韩国", "US":"美国", "GB":"英国", "FR":"法国", "DE":"德国", "IN":"印度", "TH":"泰国"}
REGION_GROUPS = [
    {"name": "国产", "countries": ["CN", "HK", "TW"]},
    {"name": "欧美", "countries": ["US", "CA", "GB", "FR", "DE", "IT", "ES", "PT", "NL", "BE", "AT", "CH", "IE", "SE", "NO", "DK", "FI", "IS", "PL", "CZ", "SK", "HU", "RO", "BG", "GR", "RU", "UA", "AU", "NZ"]},
    {"name": "日韩", "countries": ["JP", "KR"]},
]


def enabled(config):
    return config["enabled_categories"] + config["custom_categories"]


def validate(config):
    for key in ("enabled_categories", "custom_categories"):
        values = config[key]
        if not isinstance(values, list) or len(values) > 20 or len(set(str(v) for v in values)) != len(values):
            raise CloudError("分类应为最多 20 项的不重复列表")
        for value in values:
            if not isinstance(value, str) or clean(value) != value or len(value.encode()) > 80:
                raise CloudError("分类名称必须为有效的单级目录名")
            if key == "enabled_categories" and value not in DEFAULT+OPTIONAL:
                raise CloudError("内置分类无效，请在自定义分类中添加")
    all_names = enabled(config)
    if len(set(all_names)) != len(all_names):
        raise CloudError("自定义分类不能与内置分类重名")
    if config["classification_enabled"] and not all_names:
        raise CloudError("启用分类时至少选择一个分类")
    if config["region_mode"] not in ("country", "grouped"):
        raise CloudError("地区分类模式无效")
    rules = config["region_groups"]
    if not isinstance(rules, list) or not 1 <= len(rules) <= 12:
        raise CloudError("地区归组应为 1 至 12 组")
    names, codes = set(), set()
    import re
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) != {"name", "countries"}:
            raise CloudError("地区归组字段无效")
        label, countries = rule["name"], rule["countries"]
        if not isinstance(label, str) or clean(label) != label or len(label.encode()) > 40 or label == "其他" or label in names:
            raise CloudError("地区组名称无效、重复或使用了保留名称“其他”")
        if not isinstance(countries, list) or not 1 <= len(countries) <= 80:
            raise CloudError("每个地区组需要 1 至 80 个国家或地区代码")
        for code in countries:
            if not isinstance(code, str) or not re.fullmatch(r"[A-Z]{2}", code) or code in codes:
                raise CloudError("国家或地区代码须为不重复的两位大写代码")
            codes.add(code)
        names.add(label)


def decide(config, detail, kind, override=None):
    names = enabled(config)
    forced = (override or {}).get("category")
    if forced:
        if forced not in names or not config["classification_enabled"]:
            raise CloudError("所选分类未启用，请检查分类设置", 409)
        category, reason = forced, "手动指定分类"
    else:
        genre_ids = {str(x) for x in (detail.get("genre_ids") or [])}
        genre_ids.update(str(x.get("id")) for x in (detail.get("genres") or []) if isinstance(x, dict))
        category = "剧集" if kind == "tv" else "电影"
        reason = "TMDB 作品类型"
        for name, ids in (("儿童节目", {"10762"}), ("动漫", {"16"}), ("纪录片", {"99"}), ("综艺", {"10764", "10767"})):
            if name == "动漫" and kind == "movie" and config["animation_movie_folder"] and "电影" in names:
                continue
            if name in names and genre_ids & ids:
                category, reason = name, "TMDB 类型匹配"
                break
        if config["classification_enabled"] and category not in names:
            raise CloudError("作品分类未选中，请启用分类或手动修正", 409)
    countries = detail.get("origin_country") or [i.get("iso_3166_1") for i in (detail.get("production_countries") or []) if isinstance(i, dict)]
    region = next((COUNTRIES[code] for code in COUNTRIES if code in countries), "其他")
    group = next((rule["name"] for rule in config["region_groups"] if set(rule["countries"]) & set(countries)), "其他")
    animation = kind == "movie" and category == "电影" and config["animation_movie_folder"] and "16" in ({str(x) for x in (detail.get("genre_ids") or [])} | {str(x.get("id")) for x in (detail.get("genres") or []) if isinstance(x, dict)})
    subcategory = "动画电影" if animation else (group + category if config["region_mode"] == "grouped" else region)
    if animation:
        reason = "TMDB 动画电影类型匹配"
    return {"category": category, "category_reason": reason, "region": region, "region_group": group, "subcategory": subcategory, "category_override": forced or ""}


def prefix(config, media):
    if not config["classification_enabled"]:
        return config["tv_folder"] if media["kind"] == "tv" else config["movie_folder"]
    result = [media["category"]]
    if media.get("subcategory") == "动画电影" or config["region_classification"]:
        result.append(media.get("subcategory") or media["region"])
    if config["year_classification"]:
        result.append(media["year"])
    return "/".join(result)


def directory_paths(config):
    """Use the same rules for pre-creation and recognition. Never pre-create years."""
    result = []
    for category in enabled(config):
        result.append(category)
        if category == "电影" and config["animation_movie_folder"]:
            result.append(category + "/动画电影")
        if config["region_classification"]:
            labels = [r["name"]+category for r in config["region_groups"]]+["其他"+category] if config["region_mode"] == "grouped" else list(COUNTRIES.values())+["其他"]
            result.extend(category+"/"+label for label in labels)
    return result
