"""labreport: CLI-агент для формування звітів до лабораторних робіт."""
import argparse
import difflib
import json
import mimetypes
import re
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from pypdf import PdfReader
from rich.console import Console
from rich.markdown import Markdown
from rich.syntax import Syntax

HERE = Path(__file__).resolve().parent
SKIP_DIRS = {"node_modules", ".git", ".expo", "__pycache__", ".venv", "venv", "build", "dist", ".idea", ".vscode"}
TEXT_LIMIT = 30_000
IMAGE_LIMIT = 4_000_000
MAX_STEPS = 40
MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.6-flash"]

console = Console()

SYSTEM = """Ти пишеш звіт до лабораторної роботи студента за правилами предмета (профіль нижче).

Як працювати:
1. Спочатку прочитай завдання (read_pdf / read_file), потім подивись проєкт (list_dir, read_file) і скріни (view_image).
   Читай пакетами: усі файли коду одним викликом read_file, усі скріни одним view_image.
   Кожен виклик — окремий запит до API з лімітом на хвилину, тому викликів має бути якомога менше.
2. Напиши весь звіт одним викликом write_report.
3. Виклич check_report і виправ усе, що він знайшов.
4. Коротко (2–5 рядків) скажи студенту, що зроблено, і окремо — що з завдання ти НЕ знайшов у коді
   або в чому не впевнений. Не переказуй звіт.

Коли студент просить правки — роби їх через edit_report (точна заміна фрагмента) або write_report,
якщо змін багато, потім check_report. Відповідай коротко.

Правила:
- Тільки факти з коду, завдання і скрінів. Нічого не вигадуй. Якщо чогось немає — так і скажи студенту.
- Шляхи до файлів у звіті — відносно теки лаби.
- Звіт існує тільки в пам'яті, на диск його зберігає студент командою /save. Не кажи, що зберіг.
"""

TOOLS = types.Tool(function_declarations=[
    types.FunctionDeclaration(
        name="list_dir", description="Вміст теки (без node_modules, .git тощо). path — відносно теки лаби, '.' — корінь.",
        parameters_json_schema={"type": "object", "properties": {
            "path": {"type": "string"}, "recursive": {"type": "boolean"}}, "required": ["path"]}),
    types.FunctionDeclaration(
        name="read_file", description="Прочитати текстові файли з номерами рядків. Передавай усі потрібні файли одним списком.",
        parameters_json_schema={"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}}}, "required": ["paths"]}),
    types.FunctionDeclaration(
        name="read_pdf", description="Витягнути текст з PDF (наприклад, завдання до лаби).",
        parameters_json_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}),
    types.FunctionDeclaration(
        name="view_image", description="Подивитися картинки (скріншоти). Передавай усі одним списком.",
        parameters_json_schema={"type": "object", "properties": {
            "paths": {"type": "array", "items": {"type": "string"}}}, "required": ["paths"]}),
    types.FunctionDeclaration(
        name="write_report", description="Замінити весь текст звіту (Markdown).",
        parameters_json_schema={"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}),
    types.FunctionDeclaration(
        name="edit_report", description="Замінити фрагмент звіту. old — точний текст, який зустрічається у звіті рівно один раз.",
        parameters_json_schema={"type": "object", "properties": {
            "old": {"type": "string"}, "new": {"type": "string"}}, "required": ["old", "new"]}),
    types.FunctionDeclaration(
        name="check_report", description="Перевірити звіт: битi посилання на файли, порожні розділи, правила профілю.",
        parameters_json_schema={"type": "object", "properties": {}}),
])


def load_profile(name):
    path = HERE / "profiles" / f"{name}.md"
    if not path.exists():
        names = ", ".join(p.stem for p in (HERE / "profiles").glob("*.md"))
        sys.exit(f"Немає профілю '{name}'. Є: {names}")
    text = path.read_text(encoding="utf-8")
    meta = {}
    if text.startswith("---"):
        head, text = text[3:].split("---", 1)
        for line in head.strip().splitlines():
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, text.strip()


class Session:
    def __init__(self, lab, profile, task, out, hide, model, log_dir):
        self.lab = lab
        self.meta, self.profile_text = load_profile(profile)
        self.task = task
        num = re.search(r"\d+", lab.name)
        n = int(num.group()) if num else 0
        for k in ("output", "code"):
            if k in self.meta:
                self.meta[k] = self.meta[k].replace("{nn}", f"{n:02d}").replace("{n}", str(n))
        self.out = (lab / (out or self.meta.get("output", "README.md"))).resolve()
        self.hide = {(lab / h).resolve() for h in hide}
        self.models = [model] if model else MODELS
        self.client = genai.Client(http_options=types.HttpOptions(
            timeout=300_000, retry_options=types.HttpRetryOptions(attempts=1)))
        self.report = ""
        self.seen = set()
        self.based_on = set()
        self.saved = self.out.read_text(encoding="utf-8") if self.out.exists() and self.out.resolve() not in self.hide else ""
        self.history = []
        self.contents = []
        self.tokens = {"in": 0, "out": 0}
        self.log_path = log_dir / f"{datetime.now():%Y%m%d-%H%M%S}_{lab.name}.jsonl"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log("start", lab=str(lab), profile=profile, task=str(task) if task else None, out=str(self.out))

    def log(self, event, **data):
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": datetime.now().isoformat(timespec="seconds"), "event": event, **data},
                               ensure_ascii=False) + "\n")

    def resolve(self, rel):
        p = Path(rel)
        if self.task and (p == self.task or p.name == self.task.name):
            return self.task
        p = (self.lab / rel).resolve()
        root = (self.lab / self.meta.get("read_root", ".")).resolve()
        if not p.is_relative_to(root):
            raise ValueError("шлях поза дозволеною текою")
        if p in self.hide:
            raise FileNotFoundError(rel)
        return p

    def list_dir(self, path, recursive=False):
        root = self.resolve(path)
        lines = []
        walker = root.rglob("*") if recursive else root.iterdir()
        for p in sorted(walker):
            rel = p.relative_to(self.lab)
            if any(part in SKIP_DIRS for part in rel.parts) or p.resolve() in self.hide:
                continue
            lines.append(f"{rel.as_posix()}/" if p.is_dir() else f"{rel.as_posix()}  ({p.stat().st_size} B)")
            if len(lines) >= 400:
                lines.append("... (обрізано)")
                break
        return "\n".join(lines) or "(порожньо)"

    def read_file(self, paths):
        out = []
        for path in paths:
            try:
                p = self.resolve(path)
                text = p.read_text(encoding="utf-8", errors="replace")
                self.seen.add(p)
                body = "\n".join(f"{i:4} {l}" for i, l in enumerate(text.splitlines(), 1))
                if len(body) > TEXT_LIMIT:
                    body = body[:TEXT_LIMIT] + "\n... (обрізано)"
            except Exception as e:
                body = f"помилка: {type(e).__name__}: {e}"
            out.append(f"===== {path} =====\n{body}")
        return "\n\n".join(out)

    def read_pdf(self, path):
        text = "\n".join(page.extract_text() or "" for page in PdfReader(self.resolve(path)).pages)
        return text[:TEXT_LIMIT] or "(у PDF немає тексту — можливо, скан)"

    def view_image(self, paths):
        notes, parts = [], []
        for path in paths:
            try:
                p = self.resolve(path)
                data = p.read_bytes()
                if len(data) > IMAGE_LIMIT:
                    notes.append(f"{path}: завелика")
                    continue
                self.seen.add(p)
                parts.append(types.FunctionResponsePart(
                    inline_data=types.FunctionResponseBlob(
                        mime_type=mimetypes.guess_type(p.name)[0] or "image/png", data=data)))
                notes.append(f"картинка {len(parts)}: {path}")
            except Exception as e:
                notes.append(f"{path}: помилка {e}")
        return "картинки нижче в такому порядку:\n" + "\n".join(notes), parts or None

    def save(self):
        self.out.write_text(self.report, encoding="utf-8")
        if self.meta.get("export") == "docx":
            from md2docx import convert
            docx_path = self.out.with_suffix(".docx")
            figs = convert(self.report, docx_path, self.lab)
            console.print(f"[green]+ {docx_path.name} ({figs} рисунків)[/]")

    def autosave(self):
        self.log_path.with_suffix(".draft.md").write_text(self.report, encoding="utf-8")

    def drop_images(self):
        for c in self.contents:
            for p in c.parts or []:
                if p.function_response and p.function_response.parts:
                    p.function_response.parts = None
                    p.function_response.response = {"result": "(картинки переглянуто раніше, з історії прибрано)"}

    def write_report(self, content):
        self.report = content
        self.based_on = set(self.seen)
        self.autosave()
        self.drop_images()
        return f"ок, {len(content.splitlines())} рядків"

    def edit_report(self, old, new):
        n = self.report.count(old)
        if n != 1:
            return f"помилка: фрагмент знайдено {n} разів, треба рівно 1. Візьми точний текст зі звіту."
        self.report = self.report.replace(old, new)
        self.autosave()
        return "ок"

    def check_report(self):
        problems = []
        for target in re.findall(r"!?\[[^\]]*\]\(([^)\s]+)\)", self.report):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            if not (self.lab / target).exists():
                problems.append(f"посилання на неіснуючий файл: {target}")
        shots = self.meta.get("screenshots")
        if shots and (self.lab / shots).is_dir():
            for img in sorted((self.lab / shots).iterdir()):
                if img.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") and f"{shots}/{img.name}" not in self.report:
                    problems.append(f"скрін не вставлено у звіт: {shots}/{img.name}")
        code_dir, exts = self.meta.get("code"), self.meta.get("code_ext", ".js .jsx .ts .tsx .py").split()
        if code_dir and (self.lab / code_dir).is_dir():
            files = [p for p in sorted((self.lab / code_dir).rglob("*")) if p.suffix in exts]
            unread = [p.relative_to(self.lab).as_posix() for p in files if p.resolve() not in self.seen]
            late = [p.relative_to(self.lab).as_posix() for p in files
                    if p.resolve() in self.seen and p.resolve() not in self.based_on]
            if unread:
                problems.append("ти не прочитав файли коду, а описуєш проєкт — прочитай і перепиши звіт: " + ", ".join(unread[:30]))
            if late:
                problems.append("ці файли прочитано вже після написання звіту — перепиши звіт (write_report) з їх урахуванням: "
                                + ", ".join(late[:30]))
        if shots and (self.lab / shots).is_dir():
            imgs = [p for p in sorted((self.lab / shots).iterdir()) if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
            unseen = [p.name for p in imgs if p.resolve() not in self.seen]
            late = [p.name for p in imgs if p.resolve() in self.seen and p.resolve() not in self.based_on]
            if unseen:
                problems.append("ти не подивився скріни, а підписуєш їх — виклич view_image: " + ", ".join(unseen))
            if late:
                problems.append("скріни переглянуто вже після написання звіту — перепиши підписи й описи (write_report): "
                                + ", ".join(late))
        if self.meta.get("puml_sync") == "yes":
            blocks = [b.strip() for b in re.findall(r"```plantuml\n(.*?)```", self.report, flags=re.S)]
            for puml in sorted(self.lab.glob("*.puml")):
                if puml.read_text(encoding="utf-8").strip() not in blocks:
                    problems.append(f"блок plantuml не збігається дослівно з {puml.name} (або його немає) — "
                                    f"прочитай файл і встав вміст без змін")
        if self.meta.get("images") == "forbidden" and re.search(r"!\[", self.report):
            problems.append("профіль забороняє картинки у звіті")
        text_wo_code = re.sub(r"```.*?```", "код", self.report, flags=re.S)
        heads = [(m.start(), m.end(), len(m.group(1)), m.group(2)) for m in
                 re.finditer(r"^(#{2,4}) (.+)$", text_wo_code, flags=re.M)]
        for i, (_, end, level, title) in enumerate(heads):
            nxt = heads[i + 1] if i + 1 < len(heads) else None
            body = text_wo_code[end:nxt[0] if nxt else len(text_wo_code)]
            if not body.strip() and not (nxt and nxt[2] > level):
                problems.append(f"порожній розділ: {title.strip()}")
        for must in filter(None, (x.strip() for x in self.meta.get("must_read", "").split(";"))):
            p = (self.lab / must).resolve()
            if p.exists() and p not in self.seen:
                problems.append(f"обов'язково прочитай {must} перед звітом (назви мають збігатися)")
        for req in self.meta.get("required", "").split(";"):
            if req.strip() and req.strip() not in self.report:
                problems.append(f"немає обов'язкового: {req.strip()}")
        if not self.report.strip():
            problems.append("звіт порожній")
        return "\n".join(problems) or "проблем не знайдено"

    def run_tool(self, name, args):
        try:
            fn = getattr(self, name)
            res = fn(**args)
            text, parts = res if isinstance(res, tuple) else (res, None)
        except Exception as e:
            text, parts = f"помилка: {type(e).__name__}: {e}", None
        return text, parts

    def generate(self):
        config = types.GenerateContentConfig(
            system_instruction=SYSTEM + "\n\n" + self.profile_text, tools=[TOOLS], temperature=0.3,
            thinking_config=types.ThinkingConfig(thinking_level="LOW"),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        for model in self.models:
            for attempt in range(3):
                try:
                    resp = self.client.models.generate_content(model=model, contents=self.contents, config=config)
                    if model != self.models[0]:
                        self.models.remove(model)
                        self.models.insert(0, model)
                    return resp
                except Exception as e:
                    code = getattr(e, "code", None) or ("timeout" if "Timeout" in type(e).__name__ else None)
                    if code not in (429, 500, 503, 504, "timeout") and not (code == 403 and "SERVICE_DISABLED" in str(e)):
                        raise
                    if "PerDay" in str(e):
                        console.print(f"[yellow]{model}: денний ліміт вичерпано, перемикаюсь[/]")
                        self.log("api_error", model=model, code="daily_quota")
                        break
                    m = re.search(r"retryDelay\W+(\d+)", str(e))
                    wait = min(int(m.group(1)) + 2, 70) if m else 10 * (attempt + 1)
                    console.print(f"[yellow]{model}: {code}, повтор через {wait} с[/]")
                    self.log("api_error", model=model, code=code)
                    time.sleep(wait)
        raise RuntimeError("усі моделі недоступні, спробуй пізніше")

    def ask(self, text):
        self.history.append(self.report)
        self.contents.append(types.Content(role="user", parts=[types.Part(text=text)]))
        self.log("user", text=text)
        t0 = time.time()
        nudges = 0
        for _ in range(MAX_STEPS):
            with console.status("думаю..."):
                resp = self.generate()
            u = resp.usage_metadata
            self.tokens["in"] += u.prompt_token_count or 0
            self.tokens["out"] += (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)
            content = resp.candidates[0].content
            self.log("step", model=resp.model_version, tokens_in=u.prompt_token_count, tokens_out=u.candidates_token_count)
            self.contents.append(content)
            calls = [p.function_call for p in content.parts or [] if p.function_call]
            if not calls:
                problems = self.check_report() if self.report else "проблем не знайдено"
                if problems != "проблем не знайдено" and nudges < 2:
                    nudges += 1
                    console.print(f"  [dim]← check_report: {problems[:120]}[/]")
                    self.log("nudge", problems=problems[:300])
                    self.contents.append(types.Content(role="user", parts=[types.Part(
                        text=f"(Автоперевірка) Звіт ще має проблеми, виправ їх, потім знову check_report:\n{problems}")]))
                    continue
                answer = resp.text or ""
                self.log("answer", text=answer, seconds=round(time.time() - t0, 1), tokens=dict(self.tokens))
                return answer
            replies = []
            for call in calls:
                args = dict(call.args or {})
                shown = {k: (v if len(str(v)) < 60 else f"<{len(v)} файлів>" if isinstance(v, list)
                             else f"<{len(str(v))} симв.>") for k, v in args.items()}
                console.print(f"  [dim]→ {call.name} {shown}[/]")
                text, parts = self.run_tool(call.name, args)
                self.log("tool", name=call.name, args=shown, result=text[:300])
                replies.append(types.Part.from_function_response(name=call.name, response={"result": text}, parts=parts))
            self.contents.append(types.Content(role="user", parts=replies))
        return "(зупинився: забагато кроків)"


def show_diff(old, new, label):
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), "було", label, lineterm="", n=1))
    if not diff:
        console.print("[dim]звіт не змінився[/]")
        return
    plus = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    minus = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    if len(diff) > 120:
        console.print(f"[cyan]~ {label}: +{plus} −{minus} рядків[/] (весь diff: /diff)")
    else:
        console.print(Syntax("\n".join(diff), "diff", theme="ansi_dark", word_wrap=True))
        console.print(f"[cyan]~ {label}: +{plus} −{minus} рядків[/]")


HELP = """команди: /save — зберегти звіт   /show — показати звіт   /diff — різниця з файлом на диску
         /undo — відкотити останню відповідь   /stats — токени і час   /quit — вийти
все інше — повідомлення агенту (наприклад: «скороти висновок», «підпиши скрін 3 інакше»)"""


def main():
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    load_dotenv(HERE / ".env")
    ap = argparse.ArgumentParser(description="Агент для звітів до лаб")
    ap.add_argument("lab", type=Path, help="тека з лабою")
    ap.add_argument("--profile", required=True, help="профіль предмета (profiles/<name>.md)")
    ap.add_argument("--task", type=Path, help="файл завдання (pdf/md), якщо він не в теці лаби")
    ap.add_argument("--out", help="ім'я файлу звіту (за замовчуванням з профілю)")
    ap.add_argument("--hide", nargs="*", default=[], help="файли, які агент не бачить (наприклад старий README)")
    ap.add_argument("--model", help=f"модель Gemini (за замовчуванням {MODELS[0]})")
    ap.add_argument("--auto", action="store_true", help="згенерувати і зберегти без чату")
    a = ap.parse_args()

    if not os.getenv("GEMINI_API_KEY"):
        sys.exit("Немає GEMINI_API_KEY. Створи solution/.env з рядком GEMINI_API_KEY=... (ключ з aistudio.google.com)")
    lab = a.lab.resolve()
    if not lab.is_dir():
        sys.exit(f"Немає теки {lab}")
    task = a.task.resolve() if a.task else None
    s = Session(lab, a.profile, task, a.out, a.hide, a.model, HERE / "logs")
    rel_out = s.out.relative_to(lab).as_posix() if s.out.is_relative_to(lab) else s.out.name

    console.print(f"[bold]labreport[/] · {lab.name} · профіль {a.profile} · звіт → {rel_out}")
    first = f"Тека лаби: {lab.name}. Звіт: {rel_out}."
    first += f" Завдання: {task.name} (передавай у read_pdf/read_file саме цю назву)." if task else \
        " Окремого файлу завдання немає — пошукай його в теці лаби."
    if s.saved:
        first += f" У теці вже є {rel_out} — прочитай його і доповни/виправ за профілем, а не пиши з нуля."
    started = time.time()

    def talk(text):
        try:
            return s.ask(text)
        except Exception as e:
            s.log("fail", error=str(e)[:300])
            return f"**Помилка API:** {str(e)[:200]}\n\nЧернетка збережена в `logs/{s.log_path.stem}.draft.md`. Повтори повідомлення пізніше."

    console.print(Markdown(talk(first + " Напиши звіт.")))
    show_diff(s.saved, s.report, rel_out)
    if a.auto:
        s.save()
        s.log("save", seconds_total=round(time.time() - started, 1), tokens=s.tokens)
        console.print(f"[green]збережено {s.out}[/]")
        return
    console.print(f"[dim]{HELP}[/]")

    while True:
        try:
            msg = console.input("[bold green]ти>[/] ").strip()
        except (EOFError, KeyboardInterrupt):
            msg = "/quit"
        if not msg:
            continue
        if msg == "/quit":
            if s.report != s.saved:
                console.print("[yellow]є незбережені зміни — /save, або /quit ще раз[/]")
                s.saved = s.report
                continue
            s.log("quit", seconds_total=round(time.time() - started, 1), tokens=s.tokens)
            break
        elif msg == "/save":
            s.save()
            s.saved = s.report
            s.log("save", seconds_total=round(time.time() - started, 1), tokens=s.tokens)
            console.print(f"[green]збережено {s.out}[/]")
        elif msg == "/show":
            console.print(Markdown(s.report))
        elif msg == "/diff":
            console.print(Syntax("\n".join(difflib.unified_diff(
                s.saved.splitlines(), s.report.splitlines(), "на диску", "чернетка", lineterm="")), "diff", theme="ansi_dark"))
        elif msg == "/undo":
            if s.history:
                s.report = s.history.pop()
                s.contents.append(types.Content(role="user", parts=[types.Part(
                    text="(Студент відкотив твою останню зміну. Поточний звіт повернуто до попередньої версії.)")]))
                s.contents.append(types.Content(role="model", parts=[types.Part(text="Зрозумів.")]))
                s.log("undo")
                console.print("[yellow]відкочено[/]")
        elif msg == "/stats":
            console.print(f"токени: вхід {s.tokens['in']}, вихід {s.tokens['out']} · "
                          f"час сесії {round((time.time() - started) / 60, 1)} хв · лог: {s.log_path.name}")
        elif msg.startswith("/"):
            console.print(f"[dim]{HELP}[/]")
        else:
            before = s.report
            console.print(Markdown(talk(msg)))
            show_diff(before, s.report, rel_out)


if __name__ == "__main__":
    main()
