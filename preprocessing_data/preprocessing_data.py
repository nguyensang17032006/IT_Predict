"""Merge four read-only raw sources into jobs_clean.csv and salary_train.csv.

Python >=3.10. Run from anywhere with --root pointing to the IT_Predict repo.
Missing crawler timestamps remain missing. All normalization rules are explicit.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import pandas as pd

SOURCE_FILES = {
    "CareerViet": "careerviet/careerviet_raw.csv",
    "ITviec": "Itviet/itviet_raw.csv",
    "TopCV": "topcv/topcv_raw.csv",
    "VietnamWorks": "Vnwork/vnwork_raw.csv",
}
RAW_COLUMNS = [
    "job_id", "source", "title", "company", "location_raw", "locations_raw",
    "level_raw", "experience_min", "experience_max", "salary_raw_min",
    "salary_raw_max", "currency", "salary_unit", "salary_text", "skills_raw",
    "work_type_raw", "job_location_type_raw", "posted_date", "valid_through",
    "description", "requirements", "status", "first_seen", "last_seen",
    "expired_at", "collected_at", "url",
]
JOBS_COLUMNS = [
    "job_uid", "job_id", "source", "title", "company", "job_role_std",
    "location_raw", "location_std", "locations_raw", "level_raw", "level_std",
    "experience_min", "experience_max", "experience_years", "salary_raw_min",
    "salary_raw_max", "currency", "salary_unit", "salary_text", "salary_type",
    "salary_min_vnd_month", "salary_max_vnd_month", "salary_mid_vnd_month",
    "salary_min_million", "salary_max_million", "salary_mid_million",
    "skills_raw", "skills_std", "work_type_raw", "work_type_std",
    "job_location_type_raw", "job_location_type_std", "posted_date",
    "valid_through", "description", "requirements", "status", "first_seen",
    "last_seen", "expired_at", "collected_at", "url", "potential_duplicate",
    "salary_outlier_flag", "salary_display",
]
SKILL_FEATURES = {
    "python": {"Python"}, "java": {"Java"},
    "javascript": {"JavaScript", "Node.js"}, "typescript": {"TypeScript"},
    "csharp": {"C#"}, "cpp": {"C++"}, "php": {"PHP"}, "sql": {"SQL"},
    "react": {"React"}, "angular": {"Angular"}, "vue": {"Vue.js"},
    "spring": {"Spring", "Spring Boot"}, "flutter": {"Flutter"},
    "kotlin": {"Kotlin"}, "swift": {"Swift"}, "aws": {"AWS"},
    "azure": {"Azure"}, "gcp": {"GCP"}, "docker": {"Docker"},
    "kubernetes": {"Kubernetes"}, "linux": {"Linux"}, "git": {"Git"},
}
CATEGORICAL_FEATURES = ["job_role_std", "location_std", "level_std",
                        "work_type_std", "job_location_type_std"]
FEATURES = CATEGORICAL_FEATURES + ["experience_years"] + [
    f"skill_{x}" for x in SKILL_FEATURES
]
TRAIN_COLUMNS = ["job_uid", "source", "title", "company"] + FEATURES + ["salary_mid_million"]
NUMERIC_RAW = ["experience_min", "experience_max", "salary_raw_min", "salary_raw_max"]
HISTORY_COLUMNS = ["first_seen", "last_seen", "expired_at", "collected_at"]
ALIASES = {
    "job_id": ["job_id", "id", "jobId"],
    "title": ["title", "job_title", "jobTitle"],
    "company": ["company", "company_name", "companyName"],
    "location_raw": ["location_raw", "location", "job_location"],
    "level_raw": ["level_raw", "level", "job_level", "seniority"],
    "skills_raw": ["skills_raw", "skills", "skill"],
    "work_type_raw": ["work_type_raw", "work_type", "employment_type"],
    "job_location_type_raw": ["job_location_type_raw", "job_location_type", "jobLocationType"],
    "requirements": ["requirements", "job_requirements", "jobRequirement"],
    "description": ["description", "job_description", "jobDescription"],
}

def text(value):
    if value is None or value is pd.NA:
        return ""
    if isinstance(value, (float, np.floating)) and math.isnan(value):
        return ""
    return str(value).strip()

def plain(value):
    value = re.sub(r"<[^>]*>", " ", html.unescape(text(value)))
    return re.sub(r"\s+", " ", value).strip()

def fold(value):
    value = unicodedata.normalize("NFKD", plain(value)).lower().replace("đ", "d")
    return "".join(c for c in value if not unicodedata.combining(c))

def number(value):
    value = text(value)
    if not value:
        return None
    try:
        result = float(value)
    except ValueError:
        # Recognize grouping separators only when there are groups of 3 digits.
        if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", value):
            result = float(re.sub(r"[.,]", "", value))
        elif re.fullmatch(r"\d+,\d{1,2}", value):
            result = float(value.replace(",", "."))
        else:
            return None
    return result if math.isfinite(result) else None

def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def git_blob_sha(path):
    content = Path(path).read_bytes()
    return hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()

def timestamp(value):
    value = text(value)
    if not value:
        return pd.NaT
    try:
        result = pd.Timestamp(value)
        if result.tzinfo is None:
            result = result.tz_localize("Asia/Ho_Chi_Minh")
        return result.tz_convert("UTC")
    except (ValueError, TypeError, OverflowError):
        return pd.NaT

def datetime_series(series):
    return pd.Series([timestamp(x) for x in series], index=series.index,
                     dtype="datetime64[ns, UTC]")

def date_string(value):
    result = timestamp(value)
    return pd.NA if pd.isna(result) else result.tz_convert("Asia/Ho_Chi_Minh").date().isoformat()

def expiry_deadline(value):
    value = text(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        # Date-only validity is inclusive, through the end of that local day.
        result = timestamp(value)
        return result + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1) if pd.notna(result) else pd.NaT
    return timestamp(value)

def extract_id(source, url):
    patterns = {
        "CareerViet": r"\.([A-Za-z0-9]{8})\.html(?:[?#]|$)",
        "ITviec": r"-(\d+)(?:[/?#]|$)", "TopCV": r"/(\d+)\.html",
        "VietnamWorks": r"-(\d+)-jv(?:[/?#]|$)",
    }
    match = re.search(patterns[source], text(url))
    return match.group(1) if match else None

def adapt_source(path, source):
    """Map aliases + fill absent fields + type + source + UID BEFORE concat."""
    raw = pd.read_csv(path, dtype={"job_id": str, "id": str, "jobId": str}, encoding="utf-8-sig",
                      keep_default_na=False, low_memory=False)
    normalized = pd.DataFrame(index=raw.index)
    for column in RAW_COLUMNS:
        candidates = [c for c in ALIASES.get(column, [column]) if c in raw.columns]
        values = pd.Series(pd.NA, index=raw.index, dtype="object")
        for candidate in candidates:
            incoming = raw[candidate].map(lambda v: text(v) or pd.NA)
            values = values.combine_first(incoming)
        normalized[column] = values
    normalized["source"] = source
    fallback_ids = 0
    for index in normalized.index:
        jid = text(normalized.at[index, "job_id"])
        if not jid:
            jid = extract_id(source, normalized.at[index, "url"])
        if not jid:
            if not text(normalized.at[index, "url"]):
                raise ValueError(f"{path}: row {index + 2} has neither job_id nor URL")
            jid = "url_" + hashlib.sha256(text(normalized.at[index, "url"]).encode()).hexdigest()[:20]
            fallback_ids += 1
        normalized.at[index, "job_id"] = jid
    normalized["job_id"] = normalized["job_id"].astype("string")
    for column in NUMERIC_RAW:
        normalized[column] = pd.Series([number(x) for x in normalized[column]],
                                       index=normalized.index, dtype="Float64")
    for column in HISTORY_COLUMNS:
        normalized[column] = datetime_series(normalized[column])
    normalized["job_uid"] = normalized["source"] + "_" + normalized["job_id"]
    return normalized, {"rows": len(raw), "fallback_ids": fallback_ids,
                        "columns": list(raw.columns), "missing_columns": [
                            c for c in RAW_COLUMNS if not any(a in raw.columns for a in ALIASES.get(c, [c]))
                        ], "sha256": sha256(path), "git_blob_sha": git_blob_sha(path)}

LOCATION_RULES = {
    "Hà Nội": r"\bha noi\b|\bhanoi\b|\bhn\b",
    "Hồ Chí Minh": r"ho chi minh|\bhcmc?\b|\bsaigon\b|\bsai gon\b|thu duc",
    "Đà Nẵng": r"\bda nang\b|\bdanang\b",
}

def normalize_location(row):
    raw = plain(row.get("location_raw"))
    candidates = re.split(r"[;|\n]+", raw) if raw else []
    try:
        locations = json.loads(text(row.get("locations_raw")) or "[]")
        if isinstance(locations, list):
            for item in locations:
                if isinstance(item, dict):
                    # locality can be the city while region is a district.
                    values = [plain(item.get(k)) for k in ["region", "locality", "city"]]
                    recognized = [v for v in values if any(re.search(p, fold(v)) for p in LOCATION_RULES.values())]
                    region = plain(item.get("region"))
                    candidates.extend(recognized or ([region] if region and not fold(region).startswith(("quan ", "phuong ", "district ", "ward ")) else []))
    except (json.JSONDecodeError, TypeError):
        pass
    result = set()
    for candidate in candidates:
        key = fold(candidate)
        if key in {"", "unknown", "not available", "nhieu dia diem", "thanh pho khac"}:
            continue
        recognized = [label for label, pattern in LOCATION_RULES.items() if re.search(pattern, key)]
        result.update(recognized or [plain(candidate)])
    return "; ".join(sorted(result)) if result else ("Nhiều địa điểm" if fold(raw) == "nhieu dia diem" else "Unknown")

ROLE_RULES = [
    ("Project Manager", r"project manager|project management|quan ly du an|quan tri du an|delivery manager"),
    ("Product Manager", r"product manager|product owner|quan ly san pham"),
    ("Business Analyst", r"business analyst|\bba\b|phan tich nghiep vu|phan tich kinh doanh|system[s]? analyst"),
    ("Software Architect", r"(?:solution|software|technical|cloud) architect|kien truc su (?:phan mem|giai phap)"),
    ("Cybersecurity", r"cyber.?security|security (?:engineer|analyst)|bao mat|an ninh mang|an toan thong tin|pentest|devsecops"),
    ("DevOps Engineer", r"devops|site reliability|\bsre\b|platform engineer"),
    ("Cloud Engineer", r"cloud (?:engineer|infrastructure|platform)|ky su dam may"),
    ("Database Administrator", r"database administrator|\bdba\b|quan tri co so du lieu"),
    ("Network/System Engineer", r"network engineer|systems? (?:engineer|administrator)|sysadmin|quan tri (?:mang|he thong)|ky su (?:mang|he thong)"),
    ("IT Support", r"it support|technical support|help.?desk|service desk|ho tro (?:ky thuat|cntt)|it technician"),
    ("Data Scientist", r"data scientist|khoa hoc du lieu"),
    ("Data Engineer", r"data engineer|big data engineer|ky su du lieu"),
    ("Data Analyst", r"data analyst|\bbi analyst\b|phan tich du lieu|business intelligence"),
    ("Machine Learning Engineer", r"machine learning|\bml engineer\b|\bai engineer\b|artificial intelligence|genai|tri tue nhan tao|ky su ai"),
    ("QA/Tester", r"\bqa\b|\bqc\b|qaqc|tester|test engineer|testing|kiem thu|automation test"),
    ("Embedded Engineer", r"embedded|firmware|lap trinh nhung|he thong nhung"),
    ("Game Developer", r"game developer|unity developer|unreal developer|lap trinh game"),
    ("ERP/SAP", r"\bsap\b|\berp\b|\bodoo\b"),
    ("IT Consultant", r"(?:application|it|technical|solution) consultant|tu van (?:cntt|giai phap)"),
    ("Mobile Developer", r"android|ios developer|mobile (?:developer|engineer)|flutter|react native|lap trinh (?:di dong|mobile)"),
    ("Fullstack Developer", r"full[ -]?stack"),
    ("Backend Developer", r"back[ -]?end"),
    ("Frontend Developer", r"front[ -]?end"),
    (".NET Developer", r"\.net\b|(?<!\w)c#|dotnet"),
    ("PHP Developer", r"\bphp\b|laravel"),
    ("Python Developer", r"\bpython\b"),
    ("Java Developer", r"\bjava\b"),
    ("JavaScript Developer", r"javascript|node\.?js"),
    ("Software Engineer", r"software (?:engineer|developer)|\bdeveloper\b|lap trinh|ky su phan mem"),
    ("IT Manager", r"(?:it|cntt) manager|truong phong (?:it|cntt)|giam doc (?:cntt|cong nghe)|\bcto\b"),
    ("UI/UX Designer", r"\bui\b|\bux\b"),
]

def normalize_role(title):
    value = fold(title)
    for role, pattern in ROLE_RULES:
        if re.search(pattern, value):
            return role
    return "Other"

# Description rules describe duties, not a list of technologies or colleagues.
# A strong duty scores 4; supporting evidence scores 2. Title cues break ties
# only when the description itself has evidence for that occupation.
ROLE_DESCRIPTION_RULES = {
    "Project Manager": (r"project|du an|delivery", [
        (r"\b(?:manage|lead|quan ly|dieu phoi)\b.{0,55}(?:software|it |cntt|phan mem).{0,35}(?:project|du an)|(?:quan ly|dieu phoi).{0,25}du an.{0,40}(?:phan mem|cntt)", 4),
        (r"(?:project plan|ke hoach du an|project delivery|project budget|tien do du an)", 2)]),
    "Product Manager": (r"product|san pham", [
        (r"\b(?:own|define|manage|prioriti[sz]e|xay dung|quan ly)\b.{0,55}(?:product roadmap|product backlog|lo trinh san pham|backlog)", 4),
        (r"product strategy|user stories|chien luoc san pham|product discovery", 2)]),
    "Business Analyst": (r"analyst|nghiep vu|phan tich|\bba\b", [
        (r"\b(?:gather|elicit|analy[sz]e|thu thap|phan tich|lam ro)\b.{0,45}(?:business requirements|yeu cau nghiep vu|yeu cau kinh doanh)", 4),
        (r"business requirement document|\bbrd\b|\bsrs\b|functional specification|dac ta chuc nang|dac ta yeu cau", 2)]),
    "Software Architect": (r"architect|kien truc", [
        (r"\b(?:design|define|thiet ke|xay dung)\b.{0,45}(?:software architecture|system architecture|kien truc (?:phan mem|he thong)|kien truc microservices)", 4),
        (r"architecture review|architectural decisions|chuan kien truc|kien truc tong the", 2)]),
    "Cybersecurity": (r"security|\bsoc\b|bao mat|an ninh|an toan thong tin|grc", [
        (r"\b(?:detect|investigate|respond|monitor|phat hien|ngan chan|giam sat|ung pho|xu ly)\b.{0,65}(?:cyberattacks?|security incidents?|security alerts?|tan cong|su co an ninh|su co bao mat|canh bao bao mat)", 4),
        (r"\b(?:configure|manage|deploy|cau hinh|quan ly|trien khai)\b.{0,50}(?:firewall|siem|ids/ips|ips/ids|thiet bi bao mat)|penetration test|kiem thu xam nhap", 4),
        (r"(?:xay dung|trien khai|cap nhat|danh gia).{0,50}(?:co che bao mat|chinh sach bao mat|quy trinh bao mat|ha tang an ninh thong tin)|iso 27001|pci dss", 2)]),
    "DevOps Engineer": (r"devops|\bsre\b|infrastructure|ha tang", [
        (r"\b(?:build|manage|maintain|implement|design|xay dung|trien khai|quan ly)\b.{0,60}(?:ci\s*/?\s*cd|deployment pipelines?|ci pipelines?|continuous integration|continuous delivery)", 4),
        (r"infrastructure as code|\biac\b|tu dong hoa.{0,25}(?:trien khai|ha tang)|terraform|ansible", 2)]),
    "Cloud Engineer": (r"cloud|dam may", [
        (r"\b(?:design|deploy|manage|maintain|thiet ke|trien khai|quan ly|van hanh)\b.{0,55}(?:cloud infrastructure|cloud environments?|ha tang (?:cloud|dam may)|he thong cloud|cloud vmware)", 4),
        (r"cloud migration|cloud networking|capacity planning|cloud operations", 2)]),
    "Database Administrator": (r"database|\bdba\b|co so du lieu", [
        (r"\b(?:administer|manage|maintain|quan tri|van hanh)\b.{0,40}(?:databases?|co so du lieu|oracle database|sql server)", 4),
        (r"database tuning|database performance|backup.{0,25}databases?|sao luu.{0,25}co so du lieu", 2)]),
    "Network/System Engineer": (r"network|system|infra|ha tang|he thong|mang|ky thuat", [
        (r"\b(?:administer|configure|maintain|operate|troubleshoot|quan tri|cau hinh|van hanh|bao tri)\b.{0,55}(?:servers?|networks?|may chu|ha tang (?:cntt|mang)|he thong (?:mang|microsoft)|active directory|windows server)", 4),
        (r"(?:backup|sao luu).{0,25}(?:servers?|may chu)|lan/wan|vmware|network infrastructure", 2)]),
    "IT Support": (r"support|help.?desk|ho tro|it |cntt|ky thuat", [
        (r"\b(?:support|assist|ho tro|huong dan|xu ly)\b.{0,45}(?:end.users?|user support|nguoi dung|loi may tinh)|(?:cai dat|bao tri|sua chua).{0,35}(?:may tinh|may in|thiet bi cntt)", 4),
        (r"help.?desk|service desk|reset.{0,15}password|reset mat khau|support tickets?|phan quyen.{0,20}tai khoan", 2)]),
    "Data Scientist": (r"data scien|khoa hoc du lieu|model|mo hinh", [
        (r"\b(?:build|develop|train|xay dung|phat trien|huan luyen)\b.{0,45}(?:statistical models?|predictive models?|mo hinh (?:thong ke|du bao|du doan))", 4),
        (r"\b(?:build|develop|train|xay dung|phat trien|huan luyen)\b.{0,45}(?:machine learning|mo hinh hoc may)", 2),
        (r"hypothesis testing|statistical analysis|kiem dinh gia thuyet|phan tich thong ke", 2)]),
    "Data Engineer": (r"data engineer|du lieu|\betl\b|dwh", [
        (r"\b(?:build|design|develop|maintain|xay dung|thiet ke|phat trien|van hanh)\b.{0,55}(?:data pipelines?|etl|elt|data warehouse|kho du lieu|luong du lieu)", 4),
        (r"data ingestion|data orchestration|airflow|spark|hadoop", 2)]),
    "Data Analyst": (r"data|analytics|phan tich|du lieu|\bbi\b", [
        (r"\b(?:analy[sz]e|phan tich)\b.{0,30}(?:data|du lieu)", 2),
        (r"\b(?:build|develop|design|create|xay dung|phat trien|thiet ke)\b.{0,45}(?:dashboards?|bao cao phan tich|bao cao bi)|power bi|tableau", 2)]),
    "Machine Learning Engineer": (r"\bai\b|\bml\b|machine learning|computer vision|tri tue nhan tao", [
        (r"\b(?:build|develop|train|deploy|optimi[sz]e|xay dung|phat trien|huan luyen|trien khai|toi uu)\b.{0,55}(?:machine learning|deep learning|ai models?|ml models?|llms?|mo hinh ai|mo hinh hoc may|computer vision|mo hinh nhan dang)", 4),
        (r"model serving|fine.tun(?:e|ing)|pytorch|tensorflow|mlops|computer vision", 2)]),
    "QA/Tester": (r"quality assurance|quality control|kiem thu|\bqa\b|\bqc\b|test", [
        (r"\b(?:write|create|develop|execute|design|viet|xay dung|thiet ke|thuc hien)\b.{0,40}(?:test cases?|test plans?|automated tests?|automation tests?|kich ban kiem thu|kiem thu phan mem)", 4),
        (r"bug reports?|regression test|kiem thu hoi quy|bao cao loi|defects?|selenium|postman", 2)]),
    "Embedded Engineer": (r"embedded|firmware|nhung|iot", [
        (r"\b(?:develop|design|implement|phat trien|thiet ke|lap trinh)\b.{0,50}(?:firmware|embedded software|phan mem nhung|he thong nhung)", 4),
        (r"microcontrollers?|\bmcu\b|rtos|stm32|vi dieu khien", 2)]),
    "Game Developer": (r"game|unity|unreal", [
        (r"\b(?:program|code|implement|lap trinh)\b.{0,45}(?:games?|gameplay|unity|unreal)|\b(?:develop|phat trien)\b.{0,35}(?:games?).{0,40}(?:unity|unreal|c\+\+|c#)", 4),
        (r"game engine|game programming|gameplay programming", 2)]),
    "ERP/SAP": (r"sap|erp|odoo|functional consultant", [
        (r"\b(?:implement|configure|customi[sz]e|develop|trien khai|cau hinh|tuy chinh|phat trien)\b.{0,45}(?:sap|erp|odoo)", 4),
        (r"abap|sap modules?|erp implementation", 2)]),
    "IT Consultant": (r"consultant|consulting|tu van|chuyen gia", [
        (r"\b(?:advise|consult|tu van)\b.{0,45}(?:it solutions?|technical solutions?|giai phap (?:cntt|cong nghe|phan mem))", 4),
        (r"\b(?:configure|implement)\b.{0,35}(?:ibm maximo|crm systems?)|solution consulting", 2)]),
    "Mobile Developer": (r"mobile|android|ios|di dong", [
        (r"\b(?:develop|build|implement|phat trien|xay dung|lap trinh)\b.{0,45}(?:mobile apps?|mobile applications?|android|ios|ung dung (?:mobile|di dong))", 4),
        (r"flutter|react native|swift|kotlin", 2)]),
    "Fullstack Developer": (r"full.?stack", [
        (r"\b(?:develop|build|implement|phat trien|xay dung|lap trinh)\b.{0,55}full.?stack", 4)]),
    "Backend Developer": (r"back.?end|api", [
        (r"\b(?:develop|build|implement|design|phat trien|xay dung|lap trinh|thiet ke)\b.{0,55}(?:back.end|backend|server.side|restful apis?|backend apis?)", 4),
        (r"microservices|spring boot|django|nest.?js", 2)]),
    "Frontend Developer": (r"front.?end|web", [
        (r"\b(?:develop|build|implement|phat trien|xay dung|lap trinh)\b.{0,55}(?:front.end|frontend|web interfaces?|giao dien (?:web|nguoi dung))", 4),
        (r"react(?:\.?js)?|vue(?:\.?js)?|angular|html.{0,10}css", 2)]),
    ".NET Developer": (r"\.net|c#|dotnet", [
        (r"\b(?:develop|build|code|lap trinh|phat trien)\b.{0,60}(?:\.net|c#|dotnet)", 4)]),
    "PHP Developer": (r"php|laravel", [
        (r"\b(?:develop|build|code|lap trinh|phat trien)\b.{0,60}(?:\bphp\b|laravel)", 4)]),
    "Python Developer": (r"python", [
        (r"\b(?:develop|build|code|lap trinh|phat trien)\b.{0,60}\bpython\b", 4)]),
    "Java Developer": (r"\bjava\b", [
        (r"\b(?:develop|build|code|lap trinh|phat trien)\b.{0,60}\bjava\b", 4)]),
    "JavaScript Developer": (r"javascript|node\.?js", [
        (r"\b(?:develop|build|code|lap trinh|phat trien)\b.{0,60}(?:javascript|node\.?js)", 4)]),
    "Software Engineer": (r"software|phan mem|ung dung|lap trinh|engineer", [
        (r"\b(?:develop|build|implement|phat trien|xay dung|lap trinh)\b.{0,40}(?:software|phan mem|web applications?|ung dung (?:web|cntt))", 4),
        (r"write.{0,15}code|viet.{0,15}ma nguon|code review|unit tests?|object.oriented", 2)]),
    "IT Manager": (r"head of (?:it|technology)|it (?:team |)leader|cntt|truong phong|giam doc", [
        (r"\b(?:manage|lead|quan ly|dieu phoi)\b.{0,40}(?:it team|it department|doi ngu (?:it|cntt|helpdesk)|phong cntt)|technology (?:vision|strategy)|chien luoc cntt", 4),
        (r"it budget|it governance|ngan sach cntt|quan tri cntt", 2)]),
    "UI/UX Designer": (r"\bui\b|\bux\b|giao dien|product design", [
        (r"\b(?:design|create|thiet ke|xay dung)\b.{0,40}(?:user interfaces?|user experience|user flows?|wireframes?|giao dien nguoi dung|trai nghiem nguoi dung)", 4),
        (r"figma|usability testing|interactive prototypes?", 2)]),
    "Presales Engineer": (r"pre.?sales|technical sales|sales engineer", [
        (r"technical proposals?|technical workshops?|proof.of.concept|\bpoc\b|demo.{0,35}(?:giai phap|solutions?)", 2),
        (r"\b(?:consult|design|tu van|thiet ke)\b.{0,40}(?:it solutions?|technical solutions?|microsoft solutions?|giai phap (?:cntt|cong nghe|phan mem))", 2)]),
    "Bridge Engineer/IT Comtor": (r"brse|bridge engineer|it comtor", [
        (r"japanese clients?|khach hang nhat|doi tac nhat", 2),
        (r"development teams?|doi ngu phat trien|doi phat trien|translate.{0,40}(?:technical|requirements)|dich.{0,40}(?:tai lieu ky thuat|yeu cau)", 2)]),
    "Game Designer": (r"game design", [
        (r"game design document|\bgdd\b|game mechanics|level design|game economy|thiet ke.{0,25}gameplay", 4)]),
    "Scrum Master": (r"scrum master|agile coach", [
        (r"scrum ceremon|sprint planning|daily scrum|sprint retrospective|remove.{0,25}impediments?|loai bo.{0,25}(?:tro ngai|rao can)", 4)]),
}

ROLE_CHANGE_COLUMNS = ["job_uid", "source", "title", "field", "previous_value",
                       "new_value", "evidence_field", "evidence", "rule"]

def match_evidence(value, match):
    return value[max(0, match.start() - 65):min(len(value), match.end() + 100)].strip()

def positive_mention(value, match):
    """Reject direct negation and mentions of somebody else's occupation/type."""
    prefix = value[max(0, match.start() - 90):match.start()]
    if re.search(r"(?:\bnot|\bno|khong(?: phai| tuyen| ap dung| chap nhan| yeu cau)?)\s+(?:a |an |the |vi tri |cong viec |job |role |position |can |is |does |lam viec )*$", prefix):
        return False
    return not re.search(r"(?:collaborat\w* with|work\w* with|liais\w* with|coordinate with|phoi hop(?: cung| voi)?|lam viec voi|ho tro)\s+(?:(?:the|our|a|an|team|nhom|bo phan|doi ngu)\s+)*$", prefix)

def description_duties(value):
    value = fold(value)
    header = r"\b(?:yeu cau ung vien|yeu cau cong viec|yeu cau tuyen dung|job requirements?|candidate requirements?|requirements\s*:|qualifications\s*:|your skills and experience|quyen loi|what we offer|why you(?:'ll| will) love working here|nganh nghe)(?=\W|$)"
    return re.split(header, value, maxsplit=1)[0]

def duty_mention(value, match):
    if not positive_mention(value, match):
        return False
    prefix = re.split(r"[.;•]", value[max(0, match.start() - 100):match.start()])[-1]
    return not re.search(r"knowledge of|experience (?:in|with)|understanding of|ability to|familiar with|proficien\w* (?:in|with)|skills? (?:in|with)|co kha nang|co kien thuc|kien thuc ve|hieu biet ve|ky nang|kinh nghiem (?:ve|trong)|co hoi.{0,45}(?:hoc hoi|phat trien)", prefix)

def infer_job_role(row):
    """Keep recognized titles; revisit Other using positive duty evidence."""
    role = normalize_role(row.get("title"))
    if role != "Other":
        return role, None
    title, description = fold(row.get("title")), description_duties(row.get("description"))
    if not description:
        return "Other", None
    # Non-IT occupations mention software too; those mentions are not duties.
    commercial = r"marketing|kinh doanh|\bsales\b|account executive|ke toan|accountant|nhan su|human resources|graphic|do hoa|[23]d artist|motion|video|content|giao vien|teaching|telesales"
    technical_sales = bool(re.search(r"pre.?sales|technical sales|sales engineer", title))
    if re.search(commercial, title) and not technical_sales:
        return "Other", None
    candidates = []
    for candidate, (title_pattern, rules) in ROLE_DESCRIPTION_RULES.items():
        matches, score = [], 0
        for pattern, weight in rules:
            match = next((m for m in re.finditer(pattern, description) if duty_mention(description, m)), None)
            if match:
                score += weight
                matches.append(match)
        if not matches:
            continue
        title_hit = bool(re.search(title_pattern, title))
        # Generic headings cannot turn two unrelated words into a role.
        if title_hit:
            score += 5
        if candidate in {"Presales Engineer", "Bridge Engineer/IT Comtor", "Game Designer", "Scrum Master"} and not title_hit:
            continue
        if score >= 4:
            candidates.append((score, title_hit, candidate, matches[0]))
    if not candidates:
        return "Other", None
    # A specific occupation in an otherwise unrecognized title anchors the
    # result, but still needs a duty match. Skills alone never override it.
    for target, pattern in [
        ("Presales Engineer", r"pre.?sales|technical sales|sales engineer"),
        ("Scrum Master", r"scrum master|agile coach"),
        ("Data Scientist", r"data scien|khoa hoc du lieu"),
        ("Cybersecurity", r"security|an ninh thong tin|bao mat du lieu|\bsoc\b"),
        ("IT Manager", r"head of technology|(?:director|giam doc).{0,30}(?:infrastructure|ha tang)|it (?:team |)leader"),
    ]:
        if re.search(pattern, title):
            candidates = [c for c in candidates if c[2] == target]
            if not candidates:
                return "Other", None
            break
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best = candidates[0]
    tied = [c for c in candidates if c[:2] == best[:2]]
    if len(tied) > 1:
        # A clear specialization wins over generic software development.
        specialized = [c for c in tied if c[2] != "Software Engineer"]
        if len(specialized) != 1:
            return "Other", None
        best = specialized[0]
    return best[2], {"evidence_field": "description", "evidence": match_evidence(description, best[3]),
                     "rule": "description_duties_and_title" if best[1] else "description_duties"}

def normalize_level(row):
    title = fold(row.get("title")); raw = fold(row.get("level_raw"))
    for value in [title, raw]:
        for label, pattern in [
            ("Intern", r"\bintern(?:ship)?\b|thuc tap"),
            ("Fresher", r"\bfresher\b|graduate|moi tot nghiep"),
            ("Director", r"\bdirector\b|\bcto\b|giam doc"),
            ("Principal", r"\bprincipal\b"),
            ("Manager", r"\bmanager\b|head of|truong/?pho phong|truong phong|quan ly|giam sat"),
            ("Lead", r"\blead\b|\bleader\b|truong nhom"),
        ]:
            if re.search(pattern, value):
                return label
        if re.search(r"\b(mid|middle)\b", value) and re.search(r"\b(senior|sr)\b", value):
            return "Middle/Senior"
        for label, pattern in [("Senior", r"\bsenior\b|\bsr\b"),
                               ("Middle", r"\bmiddle\b|\bmid\b"),
                               ("Junior", r"\bjunior\b|\bjr\b")]:
            if re.search(pattern, value):
                return label
    return "Experienced" if raw in {"experienced", "nhan vien", "non-manager", "non manager"} else "Unknown"

def normalize_experience(row):
    lo, hi = number(row.get("experience_min")), number(row.get("experience_max"))
    lo = lo if lo is not None and 0 <= lo <= 40 else None
    hi = hi if hi is not None and 0 <= hi <= 40 else None
    if lo is not None and hi is not None and lo > hi:
        return None, None
    if lo is None and hi is None:
        value = fold(text(row.get("requirements")) or text(row.get("description")))
        if re.search(r"khong (?:yeu cau|can) kinh nghiem|no experience required", value):
            return 0.0, None
        match = re.search(r"(?:tu |from )?(\d+(?:[.,]\d+)?)\s*(?:-|–|—|den|to)\s*(\d+(?:[.,]\d+)?)\s*(?:nam|years?)", value)
        if match:
            lo, hi = number(match[1]), number(match[2])
        else:
            match = re.search(r"(?:it nhat|toi thieu|at least|minimum(?: of)?)\s*(\d+(?:[.,]\d+)?)\s*(?:nam|years?)", value)
            if not match:
                match = re.search(r"(\d+(?:[.,]\d+)?)\s*\+\s*(?:nam|years?)", value)
            if match:
                lo = number(match[1])
    if (lo is not None and not 0 <= lo <= 40) or (hi is not None and not 0 <= hi <= 40) or (lo is not None and hi is not None and lo > hi):
        return None, None
    return lo, hi

SKILLS = {
    "Python": r"\bpython\b", "Java": r"\bjava\b", "JavaScript": r"\bjava\s*script\b|\bjavascript\b|\bjs\b",
    "TypeScript": r"\btypescript\b|\btype\s*script\b", "C#": r"(?<!\w)c#(?!\w)|\bcsharp\b",
    "C++": r"(?<!\w)c\+\+(?!\w)|\bcpp\b", "PHP": r"\bphp\b", "SQL": r"\bsql\b",
    "React": r"\breact(?:\.?js)?\b", "React Native": r"\breact\s+native\b",
    "Node.js": r"\bnode\.?js\b", "Angular": r"\bangular(?:\.?js)?\b", "Vue.js": r"\bvue(?:\.?js)?\b",
    "Spring": r"\bspring\b", "Spring Boot": r"\bspring\s*boot\b", "Flutter": r"\bflutter\b",
    "Kotlin": r"\bkotlin\b", "Swift": r"\bswift\b", "Dart": r"\bdart\b", ".NET": r"\.net\b|\bdotnet\b",
    "Go": r"\bgolang\b|\bgo (?:language|programming)\b", "Rust": r"\brust\b", "Ruby": r"\bruby\b",
    "AWS": r"\baws\b|amazon web services", "Azure": r"\bazure\b", "GCP": r"\bgcp\b|google cloud",
    "Docker": r"\bdocker\b", "Kubernetes": r"\bkubernetes\b|\bk8s\b", "Linux": r"\blinux\b",
    "Git": r"\bgit\b|\bgithub\b|\bgitlab\b", "MySQL": r"\bmysql\b", "PostgreSQL": r"\bpostgres(?:ql)?\b",
    "SQL Server": r"\bsql server\b|\bmssql\b", "Oracle": r"\boracle\b", "MongoDB": r"\bmongodb\b",
    "Redis": r"\bredis\b", "Elasticsearch": r"\belasticsearch\b", "HTML": r"\bhtml[5]?\b",
    "CSS": r"\bcss[3]?\b", "Next.js": r"\bnext\.?js\b", "Nuxt.js": r"\bnuxt\.?js\b",
    "Laravel": r"\blaravel\b", "Django": r"\bdjango\b", "FastAPI": r"\bfastapi\b",
    "Flask": r"\bflask\b", "Express.js": r"\bexpress(?:\.?js)?\b", "NestJS": r"\bnest\.?js\b",
    "Jenkins": r"\bjenkins\b", "CI/CD": r"\bci\s*/\s*cd\b", "Terraform": r"\bterraform\b",
    "Ansible": r"\bansible\b", "Kafka": r"\bkafka\b", "Airflow": r"\bairflow\b",
    "Spark": r"\bspark\b", "Hadoop": r"\bhadoop\b", "TensorFlow": r"\btensorflow\b",
    "PyTorch": r"\bpytorch\b", "Scikit-learn": r"\bscikit[- ]learn\b|\bsklearn\b",
    "Pandas": r"\bpandas\b", "NumPy": r"\bnumpy\b", "Power BI": r"\bpower\s*bi\b",
    "Tableau": r"\btableau\b", "Machine Learning": r"\bmachine learning\b",
    "Deep Learning": r"\bdeep learning\b", "LLM": r"\bllms?\b|large language model",
    "RAG": r"\brag\b|retrieval augmented generation", "NLP": r"\bnlp\b|natural language processing",
    "REST API": r"\brest(?:ful)?\s+api\b", "GraphQL": r"\bgraphql\b", "SAP": r"\bsap\b",
    "Selenium": r"\bselenium\b", "Postman": r"\bpostman\b", "JMeter": r"\bjmeter\b",
    "Android": r"\bandroid\b", "iOS": r"\bios\b", "Unity": r"\bunity\b", "Unreal Engine": r"\bunreal\b",
}

def normalize_skills(row):
    raw = plain(row.get("skills_raw"))
    raw = re.sub(r"\$[0-9a-f]+\b", " ", raw, flags=re.I)
    combined = " ".join([raw, plain(row.get("title")),
                          plain(row.get("requirements")), plain(row.get("description"))])
    result = {name for name, pattern in SKILLS.items() if re.search(pattern, combined, re.I)}
    # Keep unrecognized explicitly supplied skills for dashboard analyses.
    for token in re.split(r"[,;|\n]+", raw):
        token = token.strip(" []\"'")
        if token and not any(re.search(pattern, token, re.I) for pattern in SKILLS.values()):
            result.add(token)
    return "; ".join(sorted(result, key=str.casefold))

def normalize_work_type(value):
    value = fold(value)
    if re.fullmatch(r"\d+(?:\.0)?", value):
        # VietnamWorks supplies undocumented typeWorkingId. Do not guess it.
        return "Unknown"
    result = []
    for label, pattern in [("Full-time", r"full[ _-]?time|toan thoi gian"),
                           ("Part-time", r"part[ _-]?time|ban thoi gian"),
                           ("Contract", r"contract(?:or)?|hop dong|cong tac vien"),
                           ("Internship", r"\bintern\b|internship|thuc tap"),
                           ("Temporary", r"temporary|thoi vu"), ("Freelance", r"freelance")]:
        if re.search(pattern, value):
            result.append(label)
    return "; ".join(result) if result else "Unknown"

EMPLOYMENT_PATTERNS = [
    ("Full-time", r"\bfull[ _-]?time\b|\btoan thoi gian\b"),
    ("Part-time", r"\bpart[ _-]?time\b|\bban thoi gian\b"),
    ("Contract", r"\bcontract(?:or)?\b|\bhop dong\b|\bcong tac vien\b"),
    ("Internship", r"\bintern(?:ship)?\b|\bthuc tap(?: sinh)?\b"),
    ("Temporary", r"\btemporary\b|\bthoi vu\b"),
    ("Freelance", r"\bfreelanc(?:e|er|ing)\b"),
]

def infer_work_type(row):
    """Raw -> title -> explicit description phrases, only while Unknown."""
    known = normalize_work_type(row.get("work_type_raw"))
    if known != "Unknown":
        return known, None
    for field in ["title", "description"]:
        value = fold(row.get(field))
        found = []
        for label, pattern in EMPLOYMENT_PATTERNS:
            for match in re.finditer(pattern, value):
                if not positive_mention(value, match):
                    continue
                prefix = value[max(0, match.start() - 90):match.start()]
                suffix = value[match.end():match.end() + 65]
                if field == "description":
                    if re.search(r"\b(?:experience|kinh nghiem|thesis project|previous|prior)\b", prefix) or re.search(r"^[^.;]{0,45}\b(?:experience|kinh nghiem)\b", suffix):
                        continue
                    # Signing an ordinary employment contract is not evidence
                    # that this job's employment type is Contract.
                    explicit = bool(re.search(r"(?:\btype|(?:employment|job|work|working|engagement|contract|position)\s+(?:type|arrangement|mode|basis))\s*[:=-]?\s*$|(?:hinh thuc|loai hinh|loai hop dong)(?: lam viec| cong viec| tuyen dung| hop tac)?\s*[:=-]?\s*$|(?:vi tri|tuyen dung|tuyen|looking for|hiring|seeking|working|work|lam viec)\s+(?:an? |nhan vien |ung vien |mot |the )*$", prefix))
                    headline = match.start() < 160 and bool(re.fullmatch(r"[\s|:–-]*", value[:match.start()]))
                    if label in {"Full-time", "Part-time", "Temporary"} and not (explicit or headline or re.search(r"^\s*(?:position|role|employment|job)\b", suffix)):
                        continue
                    if label == "Contract" and not (explicit or re.search(r"^\s*(?:based|basis|position|role|work|job)\b", suffix) or re.search(r"\bon (?:an? )?$", prefix)):
                        continue
                    if label == "Internship" and not (explicit or re.search(r"^\s*(?:position|role|job)\b", suffix)):
                        continue
                    if label == "Freelance" and re.search(r"(?:manage|quan ly|phoi hop|with|voi)\s+(?:(?:a|an|the|our|nhom|doi ngu)\s+)*$", prefix):
                        continue
                    if label == "Part-time" and re.search(r"(?:experience|kinh nghiem|previous|prior)\s*$", prefix):
                        continue
                found.append((label, match))
                break
        if found:
            return "; ".join(label for label, _ in found), {
                "evidence_field": field, "evidence": match_evidence(value, found[0][1]),
                "rule": "explicit_employment_type"}
    return "Unknown", None

def normalize_location_type(row):
    value = fold(row.get("job_location_type_raw"))

    # Ưu tiên giá trị có sẵn từ dữ liệu gốc
    if value:
        remote = bool(re.search(
            r"\bremote\b|telecommute|tu xa|lam tai nha|work from home",
            value
        ))

        onsite = bool(re.search(
            r"\bonsite\b|on.site|at office|in.office|van phong",
            value
        ))

        hybrid = bool(re.search(r"\bhybrid\b", value))

        labels = [
            label
            for label, hit in [
                ("Onsite", onsite),
                ("Hybrid", hybrid),
                ("Remote", remote)
            ]
            if hit
        ]

        if labels:
            return "; ".join(labels)

    # Fallback: tìm trong title, requirements và description
    value = " ".join([
        fold(row.get("title")),
        fold(row.get("requirements")),
        fold(row.get("description"))
    ])

    remote = bool(re.search(
        r"fully remote|remote (?:work|working|position)|"
        r"work from home|lam viec tu xa|lam tai nha|\bwfh\b",
        value
    ))

    onsite = bool(re.search(
        r"\bonsite\b|\bon-site\b|lam viec tai van phong",
        value
    ))

    hybrid = bool(re.search(r"\bhybrid\b", value))

    labels = [
        label
        for label, hit in [
            ("Onsite", onsite),
            ("Hybrid", hybrid),
            ("Remote", remote)
        ]
        if hit
    ]

    return "; ".join(labels) if labels else "Unknown"
def parse_salary_text(value):
    """Parse numeric pay only, without taking unrelated numbers from free text."""
    value = fold(value)
    if not value:
        return None, None, None, None
    million = bool(re.search(r"trieu|million|\btr\b|(?<=\d)tr\b|(?<=\d)\s*m\b", value))
    usd = bool(re.search(r"\busd\b|\$", value))
    vnd = bool(re.search(r"\bvnd\b|₫|\bdong\b|(?<=\d)d\b", value))
    currency = "CONFLICT" if usd and (vnd or million) else "USD" if usd else "VND" if vnd or million else None
    unit = "YEAR" if re.search(r"/\s*nam|/\s*year|per year|annual|yearly", value) else "MONTH" if re.search(r"/\s*thang|/\s*month|per month|monthly", value) else None
    if re.search(r"/gio|/hour|hourly|/ngay|/day|daily|/tuan|/week|weekly", value):
        unit = "UNSUPPORTED"
    numbers = re.findall(r"\d+(?:[.,]\d+)*", value)
    if not numbers:
        return None, None, currency, unit
    thousand = bool(re.search(r"nghin|ngan|thousand|(?<=\d)k\b", value))
    scale = 1_000_000 if million else 1_000 if thousand else 1
    # In a salary with units 'million', 10.5 / 10,5 means a decimal, not grouping.
    def value_of(token):
        n = float(token.replace(",", ".")) if scale == 1_000_000 and re.fullmatch(r"\d+[.,]\d{1,2}", token) else number(token)
        return n * scale if n is not None else None
    amounts = [value_of(token) for token in numbers]
    if len(amounts) >= 2 and re.search(r"\d[\d.,]*\s*(?:tr(?:ieu)?|usd|vnd|\$)?\s*(?:-|–|—|den|to)\s*\$?\s*\d", value):
        return amounts[0], amounts[1], currency, unit
    if re.search(r"up to|toi da|\btoi\b|den muc|maximum", value):
        return None, amounts[0], currency, unit
    if re.search(r"\bfrom\b|\btu\b|at least|minimum|tro len|\d\s*\+", value):
        return amounts[0], None, currency, unit
    if len(amounts) == 1 and (currency or re.fullmatch(r"\d[\d.,]*", value)):
        return amounts[0], amounts[0], currency, unit
    return None, None, currency, unit

def normalize_salary(row, usd_to_vnd=25000):
    lo, hi = number(row.get("salary_raw_min")), number(row.get("salary_raw_max"))
    p_lo, p_hi, p_currency, p_unit = parse_salary_text(row.get("salary_text"))
    currency = text(row.get("currency")).upper()
    unit = text(row.get("salary_unit")).upper()
    corrections = []
    if p_currency in {"VND", "USD"} and p_currency != currency:
        corrections.append(f"currency {currency or 'missing'} -> {p_currency} from explicit salary text")
        currency = p_currency
        # When currency differs, parsed amounts belong to the explicit text unit.
        if p_lo is not None or p_hi is not None:
            lo, hi = p_lo, p_hi
    elif currency not in {"VND", "USD"}:
        currency = p_currency or currency
    unit_aliases = {"MONTHLY": "MONTH", "THANG": "MONTH", "YEARLY": "YEAR", "ANNUAL": "YEAR", "NAM": "YEAR"}
    unit = unit_aliases.get(unit, unit)
    if p_unit and p_unit != unit:
        corrections.append(f"unit {unit or 'missing'} -> {p_unit} from explicit salary text")
        unit = p_unit
    if lo is None and hi is None:
        lo, hi = p_lo, p_hi
    else:
        # Scale small raw VND values only with an explicit million marker.
        if currency == "VND" and re.search(r"trieu|million|\btr\b|(?<=\d)tr\b", fold(row.get("salary_text"))):
            lo = lo * 1_000_000 if lo is not None and 0 < lo < 100000 else lo
            hi = hi * 1_000_000 if hi is not None and 0 < hi < 100000 else hi
    # Do not fill an absent second raw bound from an incompatible text range.
    # Parsed text has priority for an explicit single-ended salary label.
    if re.search(r"up to|toi da|\btoi\b|maximum", fold(row.get("salary_text"))):
        hi = hi if hi is not None else lo
        lo = None
    elif re.search(r"\bfrom\b|\btu\b|at least|minimum|tro len", fold(row.get("salary_text"))) and p_hi is None:
        lo = lo if lo is not None else hi
        hi = None
    flag = int(p_currency == "CONFLICT")
    if p_currency == "CONFLICT":
        corrections.append("conflicting currency symbols/million marker in salary text")
    if lo is None and hi is None:
        return {"salary_type": "negotiable", "salary_display": "Theo thỏa thuận",
                "salary_min_vnd_month": None, "salary_max_vnd_month": None,
                "salary_mid_vnd_month": None, "salary_outlier_flag": 0,
                "_salary_normalization_note": "; ".join(corrections)}
    if any(x is not None and x < 0 for x in [lo, hi]) or (lo is not None and hi is not None and lo > hi):
        flag = 1
    # Unknown currency/unit and ambiguous small VND pay must not become targets.
    if not unit:
        unit = p_unit or ""  # Never silently assume monthly for missing units.
    if currency not in {"VND", "USD"} or unit not in {"MONTH", "YEAR"}:
        flag = 1
        corrections.append("unsupported or missing currency/pay period")
    if currency == "VND" and unit == "MONTH" and any(x is not None and 0 < x < 100000 for x in [lo, hi]):
        flag = 1
        corrections.append("ambiguous VND amount below 100000 per month")
    invalid = flag == 1
    if invalid:
        return {"salary_type": "unknown", "salary_display": "Chưa xác định (cần kiểm tra)",
                "salary_min_vnd_month": None, "salary_max_vnd_month": None,
                "salary_mid_vnd_month": None, "salary_outlier_flag": 1,
                "_salary_normalization_note": "; ".join(corrections)}
    factor = (usd_to_vnd if currency == "USD" else 1) / (12 if unit == "YEAR" else 1)
    lo, hi = (x * factor if x is not None else None for x in [lo, hi])
    kind = "range" if lo is not None and hi is not None else "from" if lo is not None else "up_to"
    mid = (lo + hi) / 2 if kind == "range" else None
    def million_label(x):
        return f"{x / 1_000_000:g}"
    display = f"{million_label(lo)}–{million_label(hi)} triệu VND/tháng" if kind == "range" else f"{'Từ' if kind == 'from' else 'Tối đa'} {million_label(lo if lo is not None else hi)} triệu VND/tháng"
    return {"salary_type": kind, "salary_display": display,
            "salary_min_vnd_month": lo, "salary_max_vnd_month": hi,
            "salary_mid_vnd_month": mid, "salary_outlier_flag": flag,
            "_salary_normalization_note": "; ".join(corrections)}

def normalize_status(row, as_of):
    state = fold(row.get("status"))
    deadline = expiry_deadline(row.get("valid_through"))
    expired_at = timestamp(row.get("expired_at"))
    if state in {"expired", "inactive", "closed", "het han"} or pd.notna(expired_at) or (pd.notna(deadline) and deadline < as_of):
        # Derive deadline only; do not invent a crawler-observed expired_at.
        if pd.isna(expired_at) and pd.notna(deadline) and deadline < as_of:
            expired_at = deadline
        return "expired", expired_at
    if state in {"active", "open", "dang tuyen"} or pd.notna(deadline):
        return "active", expired_at
    return "unknown", expired_at

def deduplicate_within_source(jobs):
    jobs = jobs.copy()
    jobs["_observed"] = jobs["last_seen"].combine_first(jobs["collected_at"])
    jobs["_order"] = np.arange(len(jobs))
    first = jobs.groupby("job_uid")["first_seen"].min()
    jobs = jobs.sort_values(["_observed", "_order"], na_position="first", kind="stable")
    jobs = jobs.drop_duplicates(["source", "job_id"], keep="last")
    jobs["first_seen"] = jobs["job_uid"].map(first)
    return jobs.drop(columns=["_observed", "_order"]).reset_index(drop=True)

def company_key(value):
    value = fold(value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\b(?:cong ty|co phan|trach nhiem huu han|tnhh|jsc|co ltd|company limited|joint stock company|corporation|company|corp|limited|ltd)\b", " ", value)
    return re.sub(r"\s+", " ", value).strip()

def mark_cross_source_duplicates(jobs):
    jobs = jobs.copy(); jobs["potential_duplicate"] = 0
    buckets = defaultdict(list); pairs = []
    for i, row in jobs.iterrows():
        company = company_key(row["company"])
        if company and row["location_std"] not in {"Unknown", "Nhiều địa điểm"}:
            buckets[(company, row["location_std"])].append(i)
    for indices in buckets.values():
        for n, i in enumerate(indices):
            a = jobs.loc[i]
            a_title = re.sub(r"[^a-z0-9]+", " ", fold(a["title"])).strip()
            if not a_title:
                continue
            for j in indices[n + 1:]:
                b = jobs.loc[j]
                if a["source"] == b["source"]:
                    continue
                b_title = re.sub(r"[^a-z0-9]+", " ", fold(b["title"])).strip()
                similarity = SequenceMatcher(None, a_title, b_title).ratio()
                if similarity < 0.90 or a["level_std"] != b["level_std"]:
                    continue
                sa, sb = set(a["skills_std"].split("; ")) - {""}, set(b["skills_std"].split("; ")) - {""}
                skill_sim = len(sa & sb) / len(sa | sb) if sa | sb else 0
                ea, eb = number(a["experience_years"]), number(b["experience_years"])
                experience_match = ea is not None and eb is not None and abs(ea - eb) <= 1
                if skill_sim < 0.50 and not experience_match and similarity < 0.98:
                    continue
                jobs.loc[[i, j], "potential_duplicate"] = 1
                pairs.append({"job_uid_a": a["job_uid"], "job_uid_b": b["job_uid"],
                              "title_similarity": round(similarity, 4), "skill_similarity": round(skill_sim, 4)})
    return jobs, pairs

def add_skill_features(jobs):
    selected = jobs.copy()
    for name, aliases in SKILL_FEATURES.items():
        selected[f"skill_{name}"] = selected["skills_std"].map(
            lambda value: int(bool(set(text(value).split("; ")) & aliases))
        ).astype("int8")
    return selected

def build_train(jobs):
    selected = jobs.loc[jobs["salary_type"].eq("range") & jobs["salary_mid_million"].notna()].copy()
    return add_skill_features(selected)[TRAIN_COLUMNS].reset_index(drop=True)

def atomic_csv(frame, path):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temporary, index=False, encoding="utf-8-sig", na_rep="", float_format="%.10g")
    temporary.replace(path)

def run(root, output_dir, as_of, usd_to_vnd=25000, snapshot_ref="unknown", input_dir=None):
    root, output_dir = Path(root).resolve(), Path(output_dir).resolve()
    if usd_to_vnd <= 0:
        raise ValueError("USD conversion rate must be positive")
    frames, inputs, paths = [], {}, []
    for source, relative in SOURCE_FILES.items():
        path = Path(input_dir) / Path(relative).name if input_dir else root / relative
        path = path.resolve(); paths.append(path)
        frame, metadata = adapt_source(path, source)
        inputs[source] = {**metadata, "repository_path": relative}
        frames.append(frame)
    jobs = deduplicate_within_source(pd.concat(frames, ignore_index=True))
    rows, normalization_issues, classification_updates = [], [], []
    role_other_before = work_unknown_before = 0
    for row in jobs.astype(object).where(jobs.notna(), None).to_dict("records"):
        row["title"] = plain(row["title"])
        row["company"] = plain(row["company"]) or pd.NA
        row["description"] = plain(row["description"]) or pd.NA
        row["requirements"] = plain(row["requirements"]) or pd.NA
        role_before = normalize_role(row["title"])
        role_other_before += int(role_before == "Other")
        row["job_role_std"], role_evidence = infer_job_role(row)
        if role_evidence:
            classification_updates.append({"job_uid": row["job_uid"], "source": row["source"],
                "title": row["title"], "field": "job_role_std", "previous_value": role_before,
                "new_value": row["job_role_std"], **role_evidence})
        row["location_std"] = normalize_location(row)
        row["level_std"] = normalize_level(row)
        row["experience_min"], row["experience_max"] = normalize_experience(row)
        row["experience_years"] = row["experience_min"]
        row["skills_std"] = normalize_skills(row)
        work_before = normalize_work_type(row["work_type_raw"])
        work_unknown_before += int(work_before == "Unknown")
        row["work_type_std"], work_evidence = infer_work_type(row)
        if work_evidence:
            classification_updates.append({"job_uid": row["job_uid"], "source": row["source"],
                "title": row["title"], "field": "work_type_std", "previous_value": work_before,
                "new_value": row["work_type_std"], **work_evidence})
        row["job_location_type_std"] = normalize_location_type(row)
        row.update(normalize_salary(row, usd_to_vnd))
        note = row.pop("_salary_normalization_note", "")
        if note:
            normalization_issues.append({"job_uid": row["job_uid"], "currency_raw": row["currency"],
                                         "salary_unit_raw": row["salary_unit"], "salary_text": row["salary_text"],
                                         "salary_type": row["salary_type"], "normalization_note": note})
        for bound in ["min", "max", "mid"]:
            value = row[f"salary_{bound}_vnd_month"]
            row[f"salary_{bound}_million"] = value / 1_000_000 if value is not None else None
        row["status"], row["expired_at"] = normalize_status(row, as_of)
        row["posted_date"] = date_string(row["posted_date"])
        row["valid_through"] = date_string(row["valid_through"])
        rows.append(row)
    jobs = pd.DataFrame(rows)
    if jobs.empty:
        jobs = pd.DataFrame(columns=JOBS_COLUMNS)
    salary_numeric = [c for c in JOBS_COLUMNS if c.startswith("salary_") and c.endswith(("_month", "_million"))]
    for column in NUMERIC_RAW + ["experience_years"] + salary_numeric:
        jobs[column] = pd.to_numeric(jobs[column], errors="coerce").astype("Float64")
    mid = jobs["salary_mid_million"]
    jobs["salary_outlier_flag"] = jobs["salary_outlier_flag"].fillna(0).astype("int8")
    jobs.loc[(mid.notna() & ((mid < 1) | (mid > 300))), "salary_outlier_flag"] = 1
    # Mark, never delete: role-specific outer fences where sample size >=20.
    for _, group in jobs.groupby("job_role_std"):
        values = group["salary_mid_million"].dropna()
        if len(values) < 20:
            continue
        q1, q3 = values.quantile([0.25, 0.75]); iqr = q3 - q1
        if iqr > 0:
            flagged = values.index[(values < q1 - 3 * iqr) | (values > q3 + 3 * iqr)]
            jobs.loc[flagged, "salary_outlier_flag"] = 1
    jobs, pairs = mark_cross_source_duplicates(jobs)
    jobs = jobs[JOBS_COLUMNS].sort_values(["source", "job_id"], kind="stable").reset_index(drop=True)
    train = build_train(jobs)
    assert jobs["job_uid"].is_unique
    assert len(train) == int((jobs.salary_type.eq("range") & jobs.salary_mid_million.notna()).sum())
    assert train.job_uid.is_unique and train.salary_mid_million.notna().all()
    for column in HISTORY_COLUMNS:
        jobs[column] = jobs[column].map(lambda v: timestamp(v).isoformat() if pd.notna(timestamp(v)) else pd.NA)
    if any(output_dir / filename in paths for filename in ["jobs_clean.csv", "salary_train.csv"]):
        raise ValueError("Output paths must not overwrite input files")
    atomic_csv(jobs, output_dir / "jobs_clean.csv")
    atomic_csv(train, output_dir / "salary_train.csv")
    atomic_csv(pd.DataFrame(normalization_issues, columns=["job_uid", "currency_raw", "salary_unit_raw", "salary_text", "salary_type", "normalization_note"]), output_dir / "salary_normalization_issues.csv")
    atomic_csv(pd.DataFrame(classification_updates, columns=ROLE_CHANGE_COLUMNS), output_dir / "classification_updates.csv")
    unchanged = {source: sha256(path) == inputs[source]["sha256"] for source, path in zip(SOURCE_FILES, paths)}
    assert all(unchanged.values()), "Raw input changed during processing"
    report = {
        "snapshot_ref": snapshot_ref, "as_of": as_of.isoformat(),
        "usd_to_vnd_assumption": usd_to_vnd, "inputs": inputs,
        "raw_files_unchanged": unchanged, "input_rows": sum(v["rows"] for v in inputs.values()),
        "jobs_clean_rows": len(jobs), "salary_train_rows": len(train),
        "jobs_clean_columns": len(JOBS_COLUMNS), "salary_train_columns": len(TRAIN_COLUMNS),
        "within_source_duplicates_removed": sum(v["rows"] for v in inputs.values()) - len(jobs),
        "source_counts": jobs.source.value_counts().to_dict(),
        "train_source_counts": train.source.value_counts().to_dict(),
        "salary_types": jobs.salary_type.value_counts().to_dict(),
        "status_counts": jobs.status.value_counts().to_dict(),
        "job_role_counts": jobs.job_role_std.value_counts().to_dict(),
        "work_type_counts": jobs.work_type_std.value_counts().to_dict(),
        "classification_fallback": {
            "work_type_unknown_before": work_unknown_before,
            "work_type_unknown_after": int(jobs.work_type_std.eq("Unknown").sum()),
            "work_type_updated": work_unknown_before - int(jobs.work_type_std.eq("Unknown").sum()),
            "job_role_other_before": role_other_before,
            "job_role_other_after": int(jobs.job_role_std.eq("Other").sum()),
            "job_role_updated": role_other_before - int(jobs.job_role_std.eq("Other").sum()),
            "audit_file": "classification_updates.csv",
        },
        "salary_outlier_rows": int(jobs.salary_outlier_flag.sum()),
        "salary_normalization_issue_rows": len(normalization_issues),
        "potential_duplicate_rows": int(jobs.potential_duplicate.sum()),
        "cross_source_duplicate_pairs": pairs,
        "missing_values": jobs.isna().sum().astype(int).to_dict(),
        "normalization_notes": [
            "salary_text/raw salary columns are preserved; salary_display supplies Theo thỏa thuận.",
            "Unknown currency/monthly unit or ambiguous small VND values have no numeric target.",
            "Numeric employment IDs are not guessed; Unknown work types fall back to title then explicit description phrases.",
            "Other job roles fall back to description duties with title cues; unclear or conflicting evidence stays Other.",
            "Missing first_seen/last_seen/collected_at are not invented from posted_date.",
            "Status uses source status and validity deadline at as_of, not a live website check.",
            "Outliers and cross-source potential duplicates are marked, never deleted.",
            "Train is exactly salary_type=range AND salary_mid_million is non-null.",
        ],
    }
    (output_dir / "cleaning_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return jobs, train, report

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
    "--root",
    type=Path,
    default=Path(__file__).resolve().parent.parent,
    help="CareerPredict repository root"
    )
    parser.add_argument("--input-dir", type=Path, help="Optional flat directory holding the four raw CSVs")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "output")
    parser.add_argument("--as-of", default=pd.Timestamp.now(tz="Asia/Ho_Chi_Minh").isoformat())
    parser.add_argument("--usd-to-vnd", type=float, default=25000, help="Explicit analytical assumption, not a live rate")
    parser.add_argument("--snapshot-ref", default="unknown")
    args = parser.parse_args()
    as_of = timestamp(args.as_of)
    if pd.isna(as_of):
        parser.error("Invalid --as-of")
    _, _, report = run(args.root, args.output_dir, as_of, args.usd_to_vnd, args.snapshot_ref, args.input_dir)
    print(json.dumps({k: report[k] for k in ["jobs_clean_rows", "salary_train_rows", "source_counts", "train_source_counts", "salary_types", "classification_fallback", "raw_files_unchanged"]}, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
