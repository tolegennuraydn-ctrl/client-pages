"""Keeps a client's Buffer queue full without anyone's computer or chat: runs on GitHub's servers every 30 minutes.
Reads <client folder>/posting/queue.json (written by the marketing engine): a list of posts {id, due_utc, platform, channel_id, caption,
video_url, ai, status: planned|scheduled, buffer_id}. Sends every «planned» post that is still in the future to Buffer, oldest first,
until Buffer says its limit is reached. Saves what it scheduled back into queue.json. Key: repository secret BUFFER_API_KEY."""
import datetime as dt
import glob
import json
import os
import sys
import urllib.error
import urllib.request

KEY = os.environ.get("BUFFER_API_KEY", "")
if not KEY:
    sys.exit("BUFFER_API_KEY secret is missing")


def lit(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list):
        return "[" + ", ".join(lit(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{k}: {x[1:] if isinstance(x, str) and x.startswith('~') else lit(x)}" for k, x in v.items()) + "}"
    return str(v)


def gql(q):
    req = urllib.request.Request("https://api.buffer.com", data=json.dumps({"query": q}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + KEY})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            j = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{e.code} {e.read()[:200].decode('utf-8', 'replace')}") from None
    if j.get("errors"):
        raise RuntimeError(json.dumps(j["errors"])[:300])
    return j["data"]


now = dt.datetime.now(dt.timezone.utc)
bad = 0
for qf in sorted(glob.glob("*/posting/queue.json")):
    q = json.load(open(qf, encoding="utf-8"))
    full, changed = set(), False
    for p in sorted(q["posts"], key=lambda p: p["due_utc"]):
        due = dt.datetime.fromisoformat(p["due_utc"].replace("Z", "+00:00"))
        if p["status"] != "planned" or p["platform"] in full:
            continue
        if due < now + dt.timedelta(minutes=15):
            p["status"] = "missed"
            changed, bad = True, bad + 1
            print("MISSED", qf, p["id"], p["platform"])
            continue
        meta = ({"instagram": {"type": "~reel", "shouldShareToFeed": True, "isAiGenerated": bool(p.get("ai"))}} if p["platform"] == "instagram"
                else {"tiktok": {"isAiGenerated": bool(p.get("ai"))}})
        inp = {"text": p["caption"], "channelId": p["channel_id"], "schedulingType": "~automatic", "mode": "~customScheduled",
               "dueAt": due.strftime("%Y-%m-%dT%H:%M:%S.000Z"), "assets": [{"video": {"url": p["video_url"]}}], "metadata": meta}
        res = gql(f"mutation {{ createPost(input: {lit(inp)}) {{ ... on PostActionSuccess {{ post {{ id }} }} ... on MutationError {{ message }} }} }}")["createPost"]
        if res.get("post"):
            p["status"], p["buffer_id"], changed = "scheduled", res["post"]["id"], True
            print("scheduled", qf, p["id"], p["platform"])
        elif "limit" in str(res.get("message", "")).lower():
            full.add(p["platform"])
        else:
            bad += 1
            print("REFUSED", qf, p["id"], p["platform"], res.get("message"))
    if changed:
        json.dump(q, open(qf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
sys.exit(1 if bad else 0)
