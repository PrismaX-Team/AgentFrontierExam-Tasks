"""GitHub is the record of truth. Run only from the trusted default branch.
No PR code, dependencies, workflows or verifier entrypoints are executed.
"""
import hashlib
import importlib.util
import json
import urllib.parse
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import HTTPError

REPO = "PrismaX-Team/LiveDiscoveryBench-Tasks"
ROOT = f"https://github.com/{REPO}"
CONTEXT = "Contribution gate"
MARKER = "<!-- ldb-proposal-decision "
LABELS = {"approve": "proposal:approved", "changes_requested": "proposal:changes-requested", "decline": "proposal:declined", "submitted": "proposal:submitted", "needs_reapproval": "proposal:needs-reapproval"}

class Pending(ValueError):
    """Requirements not yet checkable because a maintainer step is still due; reported as pending, not failure."""

def require(condition, message):
    if not condition:
        raise ValueError(message)

def api(path, data=None, method=None):
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    require(token, "GitHub token required")
    request = Request("https://api.github.com/" + path, data=json.dumps(data).encode() if data is not None else None,
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"}, method=method)
    with urlopen(request, timeout=45) as response:
        body = response.read()
        return json.loads(body) if body else None

def gql(query, **variables):
    result = api("graphql", {"query": query, "variables": variables})
    require(not result.get("errors"), "GraphQL request failed")
    return result["data"]

def rest_pages(path):
    rows = []
    for page in range(1, 101):
        batch = api(f"repos/{REPO}/{path}{'&' if '?' in path else '?'}per_page=100&page={page}")
        require(isinstance(batch, list), "Unexpected paginated response")
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    raise ValueError("Pagination limit exceeded; refusing partial data")

def can_maintain(login):
    require(re.fullmatch(r"[A-Za-z0-9-]+(?:\[bot\])?", login or ""), "Invalid GitHub login")
    value = api(f"repos/{REPO}/collaborators/{login}/permission")
    return value["permission"] in ("admin", "write", "maintain")

DISCUSSION_FIELDS = """id number title body url updatedAt lastEditedAt closed author { login avatarUrl url }
category { slug } labels(first:100) { nodes { id name } pageInfo { hasNextPage } }"""

def discussion(number):
    require(isinstance(number, int) and number > 0, "Invalid Discussion number")
    row = gql('query($n:Int!){repository(owner:"PrismaX-Team",name:"LiveDiscoveryBench-Tasks"){discussion(number:$n){' + DISCUSSION_FIELDS + '}}}', n=number)["repository"]["discussion"]
    require(row is not None, "Discussion not found in this repository")
    require(not row["labels"]["pageInfo"]["hasNextPage"], "Too many Discussion labels")
    row["comments"] = []
    cursor = None
    while True:
        page = gql('query($n:Int!,$cursor:String){repository(owner:"PrismaX-Team",name:"LiveDiscoveryBench-Tasks"){discussion(number:$n){comments(first:100,after:$cursor){nodes{id body createdAt lastEditedAt author{login} } pageInfo{hasNextPage endCursor}}}}}', n=number, cursor=cursor)["repository"]["discussion"]["comments"]
        row["comments"].extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return row
        cursor = page["pageInfo"]["endCursor"]

def digest(row):
    # lastEditedAt means edit-and-revert also requires renewed approval.
    payload = {key: row.get(key) for key in ("title", "body", "lastEditedAt")}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

def fields(body):
    # GitHub preserves CRLF from Windows browsers; headings would otherwise keep a trailing CR.
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    result = {}
    for match in re.finditer(r"^### (.+)\n([\s\S]*?)(?=^### |\Z)", body, re.M):
        label, value = match.groups()
        label = label.split(" / ")[0]  # Earlier forms used inline bilingual headings.
        require(label not in result, "Duplicate form heading")
        result[label] = value.strip()
    return result

def validate_proposal(row):
    require(row["category"]["slug"] == "proposals" and not row["closed"], "Proposal must be open in Proposals")
    schema = json.loads(Path(__file__).with_name("proposal-schema.json").read_text())
    values = fields(row["body"])
    for field in schema["fields"]:
        value = values.get(field["label"].split(" / ")[0], "")
        if value == "_No response_":
            value = ""
        require(not field["required"] or bool(value), f"Required field missing: {field['id']}")
        require(len(value) <= field["max"], f"Field too long: {field['id']}")
        if field["id"] == "professionalUrl":
            require(re.fullmatch(r"https?://\S+", value), "Professional profile must be one http or https URL")
    metric = values.get("Metric type", "").split(" · ")[0]
    require(metric in schema["metrics"], "Invalid metric type")
    if metric in ("custom", "raw_only", "null"):
        explanation = values.get("Additional explanation", "")
        require(explanation and explanation != "_No response_", "This metric requires an explanation")
    sources = values.get("References", "")
    if sources and sources != "_No response_":
        # Authors often separate links with blank lines; only non-empty lines count.
        links = [line.strip() for line in sources.splitlines() if line.strip()]
        require(len(links) <= 20 and all(re.fullmatch(r"https?://\S+", line) for line in links), "References must be up to 20 http or https links, one per line")
    consent_body = values.get("Permissions", "")
    # Markdown checkboxes may wrap onto multiple lines after bilingual layout changes.
    checked = re.findall(r"^- \[[xX]\] ([\s\S]*?)(?=^- \[[ xX]\] |\Z)", consent_body, re.M)
    normalize = lambda text: " ".join(text.replace(" / ", " ").split())
    for label in schema["consents"]:
        # Same project, new public name. Accept the exact earlier wording without
        # editing submitted Discussions or changing their approved fingerprints.
        variants = (label, label.replace("Science Innovation Exam", "LiveDiscoveryBench"))
        require(any(normalize(variant) in [normalize(value) for value in checked] for variant in variants), "Both material permissions and publication consent are required")

BOT_LOGINS = {"github-actions", "github-actions[bot]"}
_run_cache = {}
def decision_record(row):
    records = []
    for comment in row["comments"]:
        # GraphQL reports the Actions bot as "github-actions"; REST uses "github-actions[bot]".
        if (comment.get("author") or {}).get("login") not in BOT_LOGINS or comment["lastEditedAt"] or not comment["body"].startswith(MARKER):
            continue
        try:
            record = json.loads(comment["body"].split(" -->", 1)[0][len(MARKER):])
            run_id = int(record["run"])
            if run_id not in _run_cache:
                _run_cache[run_id] = api(f"repos/{REPO}/actions/runs/{run_id}")
            run = _run_cache[run_id]
            # A bot comment or a label on its own is never approval authority.
            if (run["path"] == ".github/workflows/proposal-review.yml" and run["event"] == "workflow_dispatch"
                    and run["head_branch"] == "main" and run["head_repository"]["full_name"] == REPO
                    and run["conclusion"] == "success" and run["run_attempt"] == 1
                    and run["actor"]["login"] == run["triggering_actor"]["login"] == record["actor"]
                    and record["discussion"] == row["number"] and record["decision"] in ("approve", "changes_requested", "decline")
                    and run["created_at"] <= comment["createdAt"] <= run["updated_at"]):
                records.append((comment["createdAt"], record))
        except (KeyError, TypeError, ValueError):
            continue
    return max(records, key=lambda item: item[0])[1] if records else None

def proposal_status(row):
    record = decision_record(row)
    if row["closed"]:
        return "closed", record
    if not record:
        return "submitted", None
    if record["digest"] != digest(row):
        return "needs_reapproval", record
    return {"approve": "approved", "changes_requested": "changes_requested", "decline": "declined"}[record["decision"]], record

def approved_proposal(pr, row):
    require(row["category"]["slug"] == "proposals", "Wrong Discussion category")
    validate_proposal(row)
    status, record = proposal_status(row)
    require(status == "approved", "Proposal lacks a current trusted approval")
    require(row["author"] and row["author"]["login"].lower() == pr["user"]["login"].lower(), "PR and Proposal authors must match")
    require(record["actor"].lower() != row["author"]["login"].lower(), "Proposal self-approval is forbidden")
    return record

def proposal_number(body):
    links = re.findall(r"^Proposal:\s*(https://[^\s]+)\s*$", body or "", re.M)
    require(len(links) == 1, "Include exactly one Proposal: URL line")
    match = re.fullmatch(re.escape(ROOT) + r"/discussions/([1-9][0-9]*)", links[0])
    require(match, "Proposal must be a Discussion in this repository")
    return int(match[1])

def seats(pr, comments, permission=can_maintain):
    chosen = None
    for comment in sorted(comments, key=lambda row: (row["updated_at"], row["id"])):
        body = comment.get("body") or ""
        if not body.startswith("/reviewers") or not permission(comment["user"]["login"]):
            continue
        # An invalid newer assignment revokes the older one rather than silently retaining it.
        match = re.fullmatch(r"/reviewers domain=@([A-Za-z0-9-]+) technical=@([A-Za-z0-9-]+)\s*", body)
        require(match, "Use /reviewers domain=@LOGIN technical=@LOGIN")
        chosen = match.groups()
    require(chosen, "Maintainer reviewer assignment required")
    require(len({value.lower() for value in (*chosen, pr["user"]["login"])}) == 3, "Reviewers must be different and neither may be the PR author")
    return dict(zip(("domain", "technical"), chosen))

def approved_review(login, reviews, sha):
    relevant = [row for row in reviews if row["user"]["login"].lower() == login.lower() and row["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")]
    latest = max(relevant, key=lambda row: row["id"]) if relevant else None
    return bool(latest and latest["state"] == "APPROVED" and latest["commit_id"] == sha)

def confirmed_kind(pr):
    types = {label["name"] for label in pr["labels"] if label["name"].startswith("type:")}
    if not types:
        raise Pending("Awaiting maintainer type label / 等待维护者确认类型标签")
    require(len(types) == 1 and types <= {"type:new-task", "type:task-fix", "type:maintenance"}, "One supported type label is required")
    kind = next(iter(types))
    events = rest_pages(f"issues/{pr['number']}/events")
    relevant = [event for event in events if event["event"] in ("labeled", "unlabeled") and event.get("label", {}).get("name") == kind]
    require(relevant and relevant[-1]["event"] == "labeled" and can_maintain(relevant[-1]["actor"]["login"]), "Type must be confirmed by a maintainer")
    return kind

def structure(pr, kind):
    spec = importlib.util.spec_from_file_location("task_structure", Path(__file__).with_name("task_structure.py"))
    policy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(policy)
    candidate = Path(os.environ.get("RUNNER_TEMP", ".runtime")) / "ldb-candidate"
    candidate.mkdir(parents=True, exist_ok=True)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(candidate), *args], stderr=subprocess.DEVNULL).decode()
    if not (candidate / ".git").exists():
        git("init")
        git("remote", "add", "origin", ROOT + ".git")
    for sha in (pr["base"]["sha"], pr["head"]["sha"]):
        require(re.fullmatch(r"[0-9a-f]{40}", sha), "Invalid commit SHA")
        git("fetch", "--depth=1", "origin", sha)
    # Data checkout only. Never install dependencies or run files from this tree.
    git("-c", "core.hooksPath=/dev/null", "checkout", "--force", "--detach", pr["head"]["sha"])
    paths = git("diff", "--no-renames", "--name-only", "-z", pr["base"]["sha"], pr["head"]["sha"], "--").strip("\0").split("\0")
    existing = set(git("ls-tree", "--name-only", "-d", pr["base"]["sha"] + ":tasks").splitlines())
    tasks = policy.classify_changes([p for p in paths if p], existing, {kind})
    for task in tasks:
        policy.check_package(candidate / "tasks" / task)
    if kind == "type:task-fix":
        links = re.findall(r"^Task:\s*([a-z0-9][a-z0-9._-]*)\s*$", pr.get("body") or "", re.M)
        require(len(links) == 1 and set(links) == tasks, "Task: ID must match the existing task being repaired")

def gate(pr):
    require(pr["base"]["ref"] == "main" and pr["base"]["repo"]["full_name"] == REPO, "Task PR must target this repository main")
    require(not pr["draft"], "Draft PR is not ready")
    kind = confirmed_kind(pr)
    structure(pr, kind)
    if kind == "type:maintenance":
        return "Maintenance: ordinary GitHub review still required"
    if kind == "type:new-task":
        approved_proposal(pr, discussion(proposal_number(pr.get("body"))))
    assigned = seats(pr, rest_pages(f"issues/{pr['number']}/comments"))
    reviews = rest_pages(f"pulls/{pr['number']}/reviews")
    require(all(approved_review(login, reviews, pr["head"]["sha"]) for login in assigned.values()), "Both seats must approve the current commit")
    decisive = {}
    for review in sorted(reviews, key=lambda row: row["id"]):
        if review["state"] in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            decisive[review["user"]["login"].lower()] = review["state"]
    require("CHANGES_REQUESTED" not in decisive.values(), "Outstanding change requests remain")
    # Re-fetch key mutable input before publishing success; later events re-run the full gate.
    latest = api(f"repos/{REPO}/pulls/{pr['number']}")
    require(latest["head"]["sha"] == pr["head"]["sha"] and latest["body"] == pr["body"] and latest["labels"] == pr["labels"], "PR changed during validation; retry")
    if kind == "type:new-task":
        approved_proposal(latest, discussion(proposal_number(latest.get("body"))))
    return "Current Proposal, structure and both review seats verified"

def status(pr, state, message):
    api(f"repos/{REPO}/statuses/{pr['head']['sha']}", {"state": state, "context": CONTEXT, "description": message[:140], "target_url": f"{ROOT}/actions/runs/{os.environ['GITHUB_RUN_ID']}"})

def refresh():
    prs = rest_pages("pulls?state=open&base=main")
    # Invalidate previous green checks first. API failures never create a green result.
    for pr in prs:
        status(pr, "pending", "Rechecking live contribution requirements")
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    event = json.loads(Path(event_path).read_text()) if event_path else {}
    if event.get("discussion") and event.get("action") != "deleted":
        row = discussion(event["discussion"]["number"])
        if row["category"]["slug"] == "proposals":
            state, _ = proposal_status(row)
            display = {"approved":"approve", "declined":"decline"}.get(state, state)
            if display in LABELS and {label["name"] for label in row["labels"]["nodes"]} & set(LABELS.values()) != {LABELS[display]}:
                set_proposal_label(row, display)
                if display == "needs_reapproval":
                    notify_reapproval(row)
    failed = False
    for item in prs:
        pr = api(f"repos/{REPO}/pulls/{item['number']}")
        try:
            message = gate(pr)
            status(pr, "success", message)
        except Pending as error:
            status(pr, "pending", str(error))
        except ValueError as error:
            status(pr, "failure", str(error))
            print(f"PR #{pr['number']}: requirements unmet")
        except Exception:
            status(pr, "error", "GitHub or validation unavailable; retry required")
            failed = True
    if failed:
        raise RuntimeError("At least one GitHub check could not complete")

def notify_reapproval(row):
    """Tell the author that editing an approved Proposal suspended its approval.
    Called only when the label actually changes, so it posts once per edit."""
    author = (row.get("author") or {}).get("login")
    body = (
        (f"@{author} " if author else "") + "This Proposal was edited after approval, so the approval is suspended and any task PR "
        "referencing it will not pass Contribution gate until a maintainer runs **Proposal review** again on the current text. "
        "Reverting the edit does not restore the old approval.\n\n"
        "提案在获批后被编辑，批准已暂停；引用它的任务 PR 在维护者对当前正文重新运行 **Proposal review** 之前无法通过 Contribution gate。"
        "把内容改回去也不会恢复原批准。"
    )
    gql('mutation($id:ID!,$body:String!){addDiscussionComment(input:{discussionId:$id,body:$body}){comment{id}}}', id=row["id"], body=body)

def set_proposal_label(row, decision):
    names = set(LABELS.values())
    old = [label["id"] for label in row["labels"]["nodes"] if label["name"] in names]
    if old:
        gql('mutation($id:ID!,$labels:[ID!]!){removeLabelsFromLabelable(input:{labelableId:$id,labelIds:$labels}){clientMutationId}}', id=row["id"], labels=old)
    label = api(f"repos/{REPO}/labels/{LABELS[decision]}")
    gql('mutation($id:ID!,$labels:[ID!]!){addLabelsToLabelable(input:{labelableId:$id,labelIds:$labels}){clientMutationId}}', id=row["id"], labels=[label["node_id"]])

def next_steps(proposal_url):
    """Bilingual instructions for the author after approval. The PR link carries the
    Proposal line in its body, so the author only picks their fork and branch."""
    pr_body = urllib.parse.quote(f"Proposal: {proposal_url}\n\n### Implementation / 实现说明\n\n")
    link = f"{ROOT}/compare/main?expand=1&body={pr_body}"
    portal = "https://prismax-team.github.io/LiveDiscoveryBench-Tasks"
    contract_en, contract_zh = f"{portal}/en/contribute/task-contract/", f"{portal}/zh/contribute/task-contract/"
    guide_en, guide_zh = f"{ROOT}/blob/main/CONTRIBUTING_EN.md", f"{ROOT}/blob/main/CONTRIBUTING.md"
    skill = f"{ROOT}/tree/main/contributor/build-sie-task-package"
    template = f"{ROOT}/tree/main/tasks/_template"
    return (
        "\n\n### Next steps / 下一步\n\n"
        f"1. Read the [task contract]({contract_en}) to see what each of the five parts is for, and the [contribution guide]({guide_en}) for the full PR procedure.\n"
        f"2. Fork this repository and create a branch such as `task/<task-id>`. Copy [`tasks/_template/`]({template}) to `tasks/<task-id>/`. To let an AI coding agent help, copy the [`build-sie-task-package` skill]({skill}) into your agent's skills folder and ask it to build the package from this proposal and your materials.\n"
        f"3. Run `python3 contributor/build-sie-task-package/scripts/check_package.py tasks/<task-id>`, then open the PR from the link below: choose **compare across forks**, select your fork and branch, then **Create pull request**. The description already contains the required `Proposal:` line.\n"
        f"4. If you open the PR another way, its description must include exactly this line: `Proposal: {proposal_url}`\n\n"
        f"1. 先看[任务合同详情]({contract_zh})了解五个部分各是做什么的，再看[共建指南]({guide_zh})了解完整的 PR 流程。\n"
        f"2. Fork 本仓库并新建分支（如 `task/<task-id>`），把 [`tasks/_template/`]({template}) 复制为 `tasks/<task-id>/`。若希望 AI 编程助手协助，把 [`build-sie-task-package` Skill]({skill}) 拷进助手的 skills 目录，让它根据本提案和你的材料构建任务包。\n"
        f"3. 运行 `python3 contributor/build-sie-task-package/scripts/check_package.py tasks/<task-id>`，然后通过下面的链接创建 PR：点击 **compare across forks**，选择你的 fork 和分支，再点 **Create pull request**；正文已带上必需的 `Proposal:` 行。\n"
        f"4. 若从其他入口创建 PR，正文必须包含这一行：`Proposal: {proposal_url}`\n\n"
        f"[Create task PR / 创建任务 PR]({link})\n\n"
        "Editing this proposal invalidates the approval / 修改提案将使批准失效。"
    )

def decide():
    require(os.environ.get("GITHUB_REF") == "refs/heads/main", "Decisions run only on main")
    # Re-runs retain the original actor's privileges but read the current proposal.
    # Each decision must therefore be a fresh dispatch, never a replay of old inputs.
    require(os.environ.get("GITHUB_RUN_ATTEMPT") == "1", "Start a new Proposal review dispatch; rerunning old decisions is forbidden")
    actor = os.environ.get("GITHUB_TRIGGERING_ACTOR")
    require(actor and actor == os.environ.get("GITHUB_ACTOR"), "Decision must identify the actual initiating maintainer")
    require(can_maintain(actor), "Only maintainers may decide")
    row = discussion(int(os.environ["DISCUSSION_NUMBER"]))
    require(row["author"] and actor.lower() != row["author"]["login"].lower(), "No Proposal self-approval or self-decision")
    decision = os.environ["DECISION"]
    require(decision in ("approve", "changes_requested", "decline"), "Invalid decision")
    require(row["category"]["slug"] == "proposals", "Wrong category")
    reason = os.environ["REASON"].strip()
    require(20 <= len(reason) <= 5000, "Decision reason must contain 20–5000 characters")
    # Force skips field checks only. Permission, authorship, category and the public reason still apply.
    force = decision == "approve" and os.environ.get("FORCE", "") == "true"
    if decision == "approve":
        require(not row["closed"], "Proposal must be open in Proposals")
        if not force:
            validate_proposal(row)
    record = {"discussion": row["number"], "actor": actor, "decision": decision, "digest": digest(row), "run": os.environ["GITHUB_RUN_ID"]}
    if force:
        record["force"] = True
    body = MARKER + json.dumps(record) + " -->\n"
    body += f"@{row['author']['login']} Decision / 审核决定: **{decision}** by @{actor}\n\n{reason}\n\n"
    if force:
        body += "Form checks were skipped at the reviewer's request. / 应审核者要求，本次跳过了表单校验。\n\n"
    body += f"[Audit run / 审核运行]({ROOT}/actions/runs/{record['run']})"
    if decision == "approve":
        body += next_steps(row["url"])
    # Invalidate associated PR checks before changing approval authority.
    for pr in rest_pages("pulls?state=open&base=main"):
        status(pr, "pending", "Proposal decision changed; recheck required")
    gql('mutation($id:ID!,$body:String!){addDiscussionComment(input:{discussionId:$id,body:$body}){comment{id}}}', id=row["id"], body=body)
    set_proposal_label(row, decision)
    require(digest(discussion(row["number"])) == record["digest"], "Proposal changed during decision; approval invalid")

_avatars = {}

def identity(user):
    login = user["login"] if user else "ghost"
    image = (user.get("avatarUrl") or user.get("avatar_url")) if user else None
    if user and not image:
        # Approval records and /reviewers comments carry only the login; resolve the avatar once per login.
        if login not in _avatars:
            try:
                _avatars[login] = api(f"users/{login}").get("avatar_url")
            except HTTPError:
                _avatars[login] = None
        image = _avatars[login]
    return {"name": login, "githubLogin": login, "image": image, "githubUrl": f"https://github.com/{login}"}

def proposal_title(row):
    """Portal display title: the form's Task title field, else the Discussion title without the form prefix."""
    try:
        form = fields(row.get("body") or "")
    except ValueError:
        form = {}
    title = form.get("Task title", "").splitlines()[0].strip() if form.get("Task title") else ""
    if title and title != "_No response_":
        return title
    return re.sub(r"^\[Proposal\]\s*", "", row["title"]).strip() or row["title"]

def acceptance(row):
    return row["title"].startswith("[ACCEPTANCE]") or any(label["name"] == "acceptance" for label in (row["labels"]["nodes"] if isinstance(row["labels"], dict) else row["labels"]))

def snapshot():
    result = {"schemaVersion": 1, "repository": REPO, "updatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "proposals": [], "pullRequests": []}
    cursor = None
    while True:
        page = gql('query($cursor:String){repository(owner:"PrismaX-Team",name:"LiveDiscoveryBench-Tasks"){discussions(first:100,after:$cursor){nodes{number category{slug}} pageInfo{hasNextPage endCursor}}}}', cursor=cursor)["repository"]["discussions"]
        for item in page["nodes"]:
            if item["category"]["slug"] != "proposals":
                continue
            row = discussion(item["number"])
            if acceptance(row):
                continue
            state, record = proposal_status(row)
            result["proposals"].append({"number": row["number"], "title": proposal_title(row), "url": row["url"], "author": identity(row["author"]), "reviewers": [identity({"login": record["actor"]})] if record else [], "status": state, "updatedAt": row["updatedAt"]})
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    merged_new_tasks = {}
    for item in rest_pages("pulls?state=all&base=main"):
        if acceptance(item):
            continue
        kinds = {label["name"] for label in item["labels"]} & {"type:new-task", "type:task-fix", "type:maintenance"}
        if len(kinds) != 1 or "type:maintenance" in kinds:
            continue
        row = api(f"repos/{REPO}/pulls/{item['number']}")
        reviews = rest_pages(f"pulls/{row['number']}/reviews")
        try:
            comments = rest_pages(f"issues/{row['number']}/comments")
            assigned = seats(row, comments)
        except ValueError:
            assigned = {}
        statuses = rest_pages(f"statuses/{row['head']['sha']}")
        checks = [check for check in statuses if check["context"] == CONTEXT and check["creator"]["login"] == "github-actions[bot]"]
        state = checks[0]["state"] if checks else "pending"
        assignments = [{"role": role, "reviewer": identity({"login": login}), "approved": approved_review(login, reviews, row["head"]["sha"])} for role, login in assigned.items()]
        result["pullRequests"].append({"number": row["number"], "title": row["title"], "url": row["html_url"], "author": identity(row["user"]), "kind": "new_task" if "type:new-task" in kinds else "task_fix", "state": "merged" if row["merged"] else row["state"], "gate": state if state in ("success", "pending") else "failure", "assignments": assignments, "updatedAt": row["updated_at"]})
        if row["merged"] and "type:new-task" in kinds:
            # The merged PR that introduced tasks/<id>/ is the authoritative attribution.
            for changed in rest_pages(f"pulls/{row['number']}/files"):
                match = re.match(r"^tasks/([a-z0-9][a-z0-9._-]*)/", changed["filename"])
                if match and match[1] != "_template":
                    merged_new_tasks.setdefault(match[1], (row, assignments))
    result["tasks"] = task_entries(merged_new_tasks)
    print(json.dumps(result, ensure_ascii=False, indent=2))

def task_entries(merged_new_tasks):
    """One entry per registered task package on main. Attribution comes from the merged
    new-task PR when there is one, otherwise from attribution.json (team-registered tasks)."""
    repo_root = Path(__file__).resolve().parents[2]
    registry = {}
    registry_path = repo_root / "attribution.json"
    if registry_path.is_file():
        registry = json.loads(registry_path.read_text()).get("tasks", {})
    entries = []
    for package in sorted((repo_root / "tasks").iterdir()):
        if package.name == "_template" or not (package / "meta.json").is_file():
            continue
        meta = json.loads((package / "meta.json").read_text())
        entry = {"id": meta["id"], "title": meta["title"], "domain": meta["domain"], "source": "pull_request", "pullRequest": None, "authors": [], "reviewers": []}
        if meta["id"] in merged_new_tasks:
            row, assignments = merged_new_tasks[meta["id"]]
            entry["pullRequest"] = row["number"]
            entry["authors"] = [identity(row["user"])]
            entry["reviewers"] = [seat["reviewer"] for seat in assignments if seat["approved"]]
        elif meta["id"] in registry:
            record = registry[meta["id"]]
            entry["source"] = "attribution"
            entry["authors"] = [identity(api(f"users/{login}")) for login in record.get("authors", [])]
            entry["reviewers"] = [identity(api(f"users/{login}")) for login in record.get("reviewers", [])]
        else:
            entry["source"] = "unattributed"
        entries.append(entry)
    return entries

if __name__ == "__main__":
    {"refresh": refresh, "decide": decide, "snapshot": snapshot}[sys.argv[1]]()
