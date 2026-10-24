#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
遍历指定目录下所有 txt 文件，调用本地大模型 API 分析文章情节并打标签。

特性：
- 断点续传：已分析过的文件（按相对路径匹配）不再重复请求。
- 实时输出序号、文件名、分析结果摘要与标签。
- 每分析一个文件就增量落盘，中途中断也不丢进度。
- 大文章分段处理：超过阈值的文件切段独立分析后合并取并集。
"""

import os
import re
import sys
import json
import time
import requests

# ==================== 配置区（全部相对路径，基于脚本所在目录）====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
API_URL = "http://127.0.0.1:1234/v1/chat/completions"
MODEL_NAME = "qwen3.8-27b"
API_KEY = "qwen3.8-27b"

# 单次发送给模型的文章正文最大字符数（每段上限）
CHUNK_SIZE = 60000
# 文件超过此大小则启用分段模式
SPLIT_THRESHOLD = 64000
# max_tokens：思考模式已关闭，输出很短（JSON），1024 足够
MAX_TOKENS = 1024
# API 超时时间（秒）
API_TIMEOUT = 300
# 每个文件之间的短暂间隔，避免 API 过载
INTERVAL_SEC = 0.3

OUTPUT_JSON = os.path.join(BASE_DIR, "article_tags_llm.json")

# 合法标签值集合（用于代码层过滤，防止模型自创标签）
VALID_TAGS = {
    "绿帽", "交换", "出轨", "同性",
    "妈妈", "姑姑", "婶婶", "姨妈", "岳母",
    "姐姐", "妹妹", "嫂子", "弟媳", "大姨子", "小姨子",
    "儿媳", "女儿", "侄女", "外甥女", "亲家母",
}

# 乱伦身份标签集合（用于后处理判断）
INCEST_IDENTITY_TAGS = {
    "妈妈", "姑姑", "婶婶", "姨妈", "岳母",
    "姐姐", "妹妹", "嫂子", "弟媳", "大姨子", "小姨子",
    "儿媳", "女儿", "侄女", "外甥女", "亲家母",
}

# ==================== 提示词 ====================
PROMPT_TEMPLATE = """请分析文中出现了哪些性关系/乱伦/NTR情节，按规则打标签。

【定义】
- **出轨**：已婚/有伴侣者与"非亲戚"异性发生性关系（同事、领导、邻居等）。跟亲戚的性爱不算出轨，只打乱伦身份标签。
- **绿帽**：男方接受/鼓励/目睹妻子或女友与其他男人发生性关系。
- **交换**：双方互换配偶/伴侣进行性爱。
- **同性**：同性别之间发生性关系。
- **乱伦身份**：男性与女性亲戚发生性关系时，打具体身份标签（见合法值）。

【通用规则】
- 表、堂、继、干均视为直系：表姐→"姐姐"，继母→"妈妈"，干女儿→"女儿"。
- 女友视同妻子：儿子的女友→"儿媳"。
- 妻子/女友的亲戚视为自己的亲戚：妻子的姑妈→"姑姑"，妻子的嫂子→"嫂子"。**例外**：妻子/女友的妈妈→"岳母"，姐姐→"大姨子"，妹妹→"小姨子"。

【视角规则】
以**实际与女方发生性关系的男方（插入方）的视角打身份标签——他如何称呼该女人。文章主角≠实际行房者时，不要从主角视角打标。
- 例：男主让女友跟女友的父亲行房 → 父亲称其为"女儿" → ["女儿"]。
- 例：A目睹母亲与其他男人B行房 → 按B的视角打标（B如何称呼A的母亲？如果无亲戚关系则打“出轨";有亲戚关系则打对应的乱伦标签），而非从A的视角打"妈妈"。
- 例：A让妻子跟公公行房 + A自己又与岳母行房 → ["儿媳","岳母"]（两段各打各的）。

【打标规则】
- 一篇可多标签；多段乱伦关系每段独立打标后合并。
- 仅实际发生性行为（含手淫、口交、颜射等边缘性行为）才打标。
- 跟亲戚只打身份标签，不打"出轨"。
- 文中明确将女方"视如己出/当作亲生女儿"时，按"女儿"打标。
- 无上述情节则返回空数组 []。

【合法值】（不得自创）：
绿帽、交换、出轨、同性、妈妈、姑姑、婶婶、姨妈、岳母、姐姐、妹妹、嫂子、弟媳、大姨子、小姨子、儿媳、女儿、侄女、外甥女、亲家母

【返回格式】（严格 JSON，无其他文字）：
{{"tags": ["标签1","标签2"], "summary": "一句话情节描述(100字内)"}}"""

# ==================== 分段提示词（用于大文章每段独立分析）====================
CHUNK_PROMPT = """以下是长篇小说的第 {chunk_idx}/{total_chunks} 部分。请分析文中出现了哪些性关系/乱伦/NTR情节，按规则打标签。

【定义】
- **出轨**：已婚/有伴侣者与"非亲戚"异性发生性关系（同事、领导、邻居等）。跟亲戚的性爱不算出轨，只打乱伦身份标签。
- **绿帽**：男方接受/鼓励/目睹妻子或女友与其他男人发生性关系。
- **交换**：双方互换配偶/伴侣进行性爱。
- **同性**：同性别之间发生性关系。
- **乱伦身份**：男性与女性亲戚发生性关系时，打具体身份标签（见合法值）。

【通用规则】
- 表、堂、继、干均视为直系：表姐→"姐姐"，继母→"妈妈"，干女儿→"女儿"。
- 女友视同妻子：儿子的女友→"儿媳"。
- 妻子/女友的亲戚视为自己的亲戚：妻子的姑妈→"姑姑"，妻子的嫂子→"嫂子"。**例外**：妻子/女友的妈妈→"岳母"，姐姐→"大姨子"，妹妹→"小姨子"。

【视角规则】
以**实际与女方发生性关系的男方（插入方）的视角打身份标签——他如何称呼该女人。文章主角≠实际行房者时，不要从主角视角打标。
- 例：男主让女友跟女友的父亲行房 → 父亲称其为"女儿" → ["女儿"]。
- 例：A目睹母亲与其他男人B行房 → 按B的视角打标（B如何称呼A的母亲？如果无亲戚关系则打“出轨";有亲戚关系则打对应的乱伦标签），而非从A的视角打"妈妈"。
- 例：A让妻子跟公公行房 + A自己又与岳母行房 → ["儿媳","岳母"]（两段各打各的）。

【打标规则】
- 一篇可多标签；多段乱伦关系每段独立打标后合并。
- 仅实际发生性行为（含手淫、口交、颜射等边缘性行为）才打标。
- 跟亲戚只打身份标签，不打"出轨"。
- 文中明确将女方"视如己出/当作亲生女儿"时，按"女儿"打标。
- 无上述情节则返回空数组 []。

【合法值】（不得自创）：
绿帽、交换、出轨、同性、妈妈、姑姑、婶婶、姨妈、岳母、姐姐、妹妹、嫂子、弟媳、大姨子、小姨子、儿媳、女儿、侄女、外甥女、亲家母

【返回格式】（严格 JSON，无其他文字）：
{{"tags": ["标签1","标签2"], "summary": "一句话情节描述(100字内)"}}"""

def load_existing():
    """加载已有的分析结果，返回 {rel_path: entry} 字典"""
    if not os.path.exists(OUTPUT_JSON):
        print("[启动] 无历史 JSON，从头开始")
        return {}
    try:
        with open(OUTPUT_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        existing = {}
        for art in data.get("articles", []):
            if art.get("rel_path"):
                existing[art["rel_path"]] = art
        print(f"[启动] 已加载历史结果 {len(existing)} 条")
        return existing
    except Exception as e:
        print(f"[启动] 读取历史 JSON 失败（{e}），视为空数据重新开始")
        return {}


def save_results(entries, total_files):
    """增量保存：把当前所有条目写入 JSON"""
    arts = [v for v in entries.values() if v is not None]
    arts.sort(key=lambda x: x["rel_path"])
    output_data = {
        "base_dir": ".",
        "model": MODEL_NAME,
        "total_files": total_files,
        "tagged_files": sum(1 for a in arts if a.get("tags")),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "articles": arts,
    }
    tmp = OUTPUT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUTPUT_JSON)


def read_file_text(filepath):
    """读取文件完整内容，自动尝试多种编码"""
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            with open(filepath, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, IOError):
            continue
    return None


def _call_api(prompt):
    """底层 API 调用，返回原始 content 字符串"""
    payload = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.05,
        "max_tokens": MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    resp = requests.post(API_URL, json=payload, headers=headers, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices") or []
    if not choices:
        return None
    msg = choices[0].get("message", {})
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "").strip()
    if not content and reasoning:
        content = reasoning.strip()
    return content


def _extract_json(content):
    """从模型输出中提取 JSON 字符串"""
    json_str = None
    if "```" in content:
        for blk in content.split("```"):
            blk = blk.strip()
            if blk.startswith("json"):
                blk = blk[4:].strip()
            if blk and blk[0] == "{":
                json_str = blk
                break
    if not json_str:
        s = content.find("{")
        e = content.rfind("}")
        if s != -1 and e > s:
            json_str = content[s:e + 1]
    return json_str


def _parse_json(json_str):
    """解析 JSON，含多级兜底"""
    def _try_parse(s):
        try:
            v = json.loads(s)
            if isinstance(v, dict):
                return v
        except Exception:
            pass
        fixed = re.sub(r'(?m)^(\s*"[a-zA-Z_]+"|^\s*)\s*:\s*(\S.*)$',
                       lambda m: m.group(1) + ': "' + m.group(2).rstrip().strip('"') + '"', s)
        if fixed != s:
            try:
                v = json.loads(fixed)
                if isinstance(v, dict):
                    return v
            except Exception:
                pass
        m = re.search(r'"tags"\s*:\s*\[([^\]]*)\]', s)
        if m:
            inner = m.group(1)
            items = [x.strip().strip('"').strip() for x in inner.split(",") if x.strip()]
            sm = re.search(r'"summary"\s*:\s*"([^"]*)"', s)
            return {"tags": items, "summary": sm.group(1) if sm else ""}
        return None

    parsed = _try_parse(json_str)
    if parsed is None:
        print(f"  [JSON解析失败] | 片段：{json_str[:150]}")
        return None
    return parsed


def _clean_tags(tags):
    """清洗标签列表"""
    seen = set()
    clean_tags = []
    for t in tags:
        t = str(t).strip()
        if not t or t in seen:
            continue
        if t not in VALID_TAGS:
            continue
        seen.add(t)
        clean_tags.append(t)
    return clean_tags


def ask_llm_single(text, filename):
    """单次分析（小文件直接走这条路）"""
    prompt = PROMPT_TEMPLATE + "\n\n文章标题：" + os.path.basename(filename) + "\n文章内容：\n" + text

    try:
        content = _call_api(prompt)
    except Exception as e:
        print(f"  [API错误] {e}")
        return None

    if not content:
        print("  [模型返回空内容]")
        return None

    json_str = _extract_json(content)
    if not json_str:
        print(f"  [未找到JSON块，原始返回前200字：{content[:200]}]")
        return None

    parsed = _parse_json(json_str)
    if parsed is None:
        return None

    tags = parsed.get("tags", [])
    if not isinstance(tags, list):
        tags = [str(tags)]
    clean_tags = _clean_tags(tags)
    summary = str(parsed.get("summary", ""))[:100]
    return {"tags": clean_tags, "summary": summary}


def ask_llm_chunked(text, filename):
    """分段分析（大文件走这条路）：切段→每段独立问→合并取并集"""
    total_chunks = (len(text) + CHUNK_SIZE - 1) // CHUNK_SIZE
    all_tags = []
    summaries = []

    for i in range(total_chunks):
        start = i * CHUNK_SIZE
        end = min(start + CHUNK_SIZE, len(text))
        chunk_text = text[start:end]

        prompt = CHUNK_PROMPT.format(chunk_idx=i + 1, total_chunks=total_chunks) + \
                 f"\n\n文章标题：{os.path.basename(filename)}\n本段内容（第{i+1}部分，共{total_chunks}部分）：\n" + chunk_text

        try:
            content = _call_api(prompt)
        except Exception as e:
            print(f"  [API错误-段{i+1}] {e}")
            continue

        if not content:
            continue

        json_str = _extract_json(content)
        if not json_str:
            continue

        parsed = _parse_json(json_str)
        if parsed is None:
            continue

        tags = parsed.get("tags", [])
        if not isinstance(tags, list):
            tags = [str(tags)]
        clean_tags = _clean_tags(tags)
        all_tags.extend(clean_tags)

        sm = str(parsed.get("summary", "")).strip()
        if sm:
            summaries.append(sm)

    # 合并：取并集，保序去重
    seen = set()
    merged_tags = []
    for t in all_tags:
        if t not in seen:
            seen.add(t)
            merged_tags.append(t)

    # 后处理：如果有乱伦身份标签，确保不混入"出轨"（跟亲戚的性爱不算出轨）
    has_incest = any(t in INCEST_IDENTITY_TAGS for t in merged_tags)
    if has_incest and "出轨" in merged_tags:
        # 保留出轨（可能同时存在非亲戚的出轨），但让模型在最终 summary 中区分
        pass

    # 合并 summary：用 LLM 将各段摘要合并为一句100字内的总述
    if summaries:
        combined_input = "；".join(summaries)
        merge_prompt = f"请将以下小说各段落的情节简述合并为一句话（100字内），概括整篇文章的核心情节。只输出这句话，不要其他文字。\n\n{combined_input}"
        try:
            final_summary = _call_api(merge_prompt)
            if final_summary and len(final_summary.strip()) > 2:
                combined_summary = final_summary.strip()[:100]
            else:
                combined_summary = "；".join(summaries)[:100]
        except Exception:
            combined_summary = "；".join(summaries)[:100]
    else:
        combined_summary = ""

    return {"tags": merged_tags, "summary": combined_summary}


def analyze_file(filepath):
    """根据文件大小选择单次或分段分析"""
    text = read_file_text(filepath)
    if not text:
        return None

    if len(text) <= SPLIT_THRESHOLD:
        return ask_llm_single(text, filepath)
    else:
        chunks = (len(text) + CHUNK_SIZE - 1) // CHUNK_SIZE
        print(f" [分段x{chunks}]", end="", flush=True)
        return ask_llm_chunked(text, filepath)


def main():
    print("=" * 70)
    print(f"目录：{BASE_DIR}")
    print(f"模型：{MODEL_NAME}")
    print(f"输出：{OUTPUT_JSON}")
    print(f"分段阈值：{SPLIT_THRESHOLD} chars | 每段上限：{CHUNK_SIZE} chars")
    print("=" * 70)

    # 收集所有 txt 文件（相对路径作为唯一键）
    all_files = []
    for root, dirs, files in os.walk(BASE_DIR):
        for name in sorted(files):
            if name.lower().endswith(".txt"):
                fp = os.path.join(root, name)
                rel = os.path.relpath(fp, BASE_DIR).replace("\\", "/")
                all_files.append({"path": fp, "rel_path": rel})

    # 按相对路径排序，保证每次运行顺序一致（断点续传的关键）
    all_files.sort(key=lambda x: x["rel_path"])
    total = len(all_files)
    print(f"共发现 {total} 个 txt 文件\n")

    existing = load_existing()
    done_before = len(existing)
    todo_count = total - done_before
    print(f"历史已完成：{done_before} | 待分析：{todo_count}\n")

    processed_in_this_run = 0
    skipped_already_done = 0
    failed_in_this_run = 0
    start_time = time.time()

    for idx, info in enumerate(all_files, start=1):
        rel = info["rel_path"]
        fname = os.path.basename(info["path"])

        if rel in existing:
            entry = existing[rel]
            tags_str = ",".join(entry.get("tags", [])) or "(无)"
            summary = (entry.get("summary") or "").strip()
            print(f"[{idx}/{total}] {fname}  [跳过-已完成] {tags_str}" + (f" | {summary}" if summary else ""))
            skipped_already_done += 1
            continue

        # ---- 需要分析（新文件）----
        print(f"[{idx}/{total}] {fname}", end="", flush=True)
        result = analyze_file(info["path"])
        file_size = os.path.getsize(info["path"])

        if result is None:
            existing[rel] = {"filename": fname, "rel_path": rel,
                             "tags": [], "summary": "(分析失败)", "size": file_size}
            print(f"  -> [失败]")
            failed_in_this_run += 1
        else:
            existing[rel] = {"filename": fname, "rel_path": rel,
                             "tags": result["tags"], "summary": result.get("summary", ""),
                             "size": file_size}
            tags_str = ",".join(result["tags"]) or "(无)"
            summary = (result.get("summary") or "").strip()
            print(f"  -> {tags_str}" + (f" | {summary}" if summary else ""))

        processed_in_this_run += 1
        save_results(existing, total)
        time.sleep(INTERVAL_SEC)

    elapsed = time.time() - start_time
    print("\n" + "=" * 70)
    print(f"本轮新分析：{processed_in_this_run} | 跳过已完成：{skipped_already_done} | 失败：{failed_in_this_run}")
    print(f"总条目：{total} | 有标签文件数："
          f"{sum(1 for v in existing.values() if v and v.get('tags'))}")
    print(f"耗时：{elapsed / 60:.1f} 分钟")
    print(f"结果已保存至：{OUTPUT_JSON}")
    print("=" * 70)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[用户中断] 已增量保存进度，重启后可继续。")
        sys.exit(130)
