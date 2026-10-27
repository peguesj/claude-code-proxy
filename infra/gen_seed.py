import os
#!/usr/bin/env python3
"""Generate infra/ccp_groups.seed.json from compact tables (seed plan)."""
import json, sys
PREFIX = os.environ.get("CCP_ACCOUNT_PREFIX", "example-ccp")  # account names become global subdomains
LEGACY_RG = os.environ.get("CCP_LEGACY_RG", "rg-legacy-aoai")
LEGACY_ACCOUNT = os.environ.get("CCP_LEGACY_ACCOUNT", "legacy-aoai")
def m(n, d, v, s, c, f="OpenAI", k="chat"):
    return {"name": n, "deployment": d, "version": v, "sku": s, "capacity": c, "format": f, "kind": k}
G, S, D, P = "GlobalStandard", "Standard", "DataZoneStandard", "GlobalProvisionedManaged"
def grp(i, rg, region, acct, purpose, models, policy="always_on", billing="pay_per_token"):
    return {"id": i, "resource_group": rg, "region": region, "account": acct, "purpose": purpose,
            "billing": billing, "policy": policy, "idle_teardown_minutes": 60, "models": models}
legacy = [
 m("gpt-4.1","gpt-4-1","2025-04-14",S,5000), m("o4-mini","o4-mini","2025-04-16",S,10000),
 m("gpt-4o","gpt-4o","2024-11-20",S,1000), m("gpt-4.1-mini","gpt-4-1-mini","2025-04-14",S,75120),
 m("o3-mini","o3-mini","2025-01-31",G,7249), m("gpt-5","gpt-5","2025-08-07",G,134750),
 m("gpt-5-mini","gpt-5-mini","2025-08-07",G,45000), m("o3","o3","2025-04-16",G,15000),
 m("gpt-5.2","gpt-5-2","2025-12-11",G,135000), m("gpt-5.2-codex","gpt-5-2-codex","2026-01-14",G,44998),
 m("gpt-4.1-nano","gpt-4-1-nano","2025-04-14",G,675000), m("codex-mini","codex-mini","2025-05-16",G,15000),
 m("gpt-4.1-mini","gpt-4o-mini","2025-04-14",S,75120), m("o1","o1","2024-12-17",S,100),
 m("model-router","model-router","2025-11-18",G,44800,"OpenAI","router"),
 m("gpt-chat-latest","gpt-5-chat","2026-05-05",G,4079), m("gpt-chat-latest","gpt-5-1-chat","2026-05-05",G,80),
 m("gpt-chat-latest","gpt-5-2-chat","2026-05-05",G,80), m("gpt-5-nano","gpt-5-nano","2025-08-07",D,225000),
 m("gpt-5.1","gpt-5-1","2025-11-13",D,14990), m("gpt-audio","gpt-audio","2025-08-28",G,30000,"OpenAI","audio"),
 m("gpt-audio-mini","gpt-audio-mini","2025-12-15",G,30,"OpenAI","audio"),
 m("o3-mini","o3-mini-deployment","2025-01-31",G,1), m("gpt-5.3-codex","gpt-5.3-codex","2026-02-24",D,45000),
 m("gpt-5.1-codex-max","gpt-5.1-codex-max","2025-12-04",G,44999), m("gpt-5.5","gpt-5.5","2026-04-24",G,44950),
 m("gpt-5.6-luna","gpt-5.6-luna","2026-07-09",G,15000), m("gpt-5.6-terra","gpt-5.6-terra","2026-07-09",G,15000),
 m("gpt-5.6-sol","gpt-5.6-sol","2026-07-09",G,15000), m("gpt-6-astra","gpt-6-astra","2026-09-03",G,15000),
 m("gpt-6-sol","gpt-6-sol","2026-09-22",G,15000),
 m("gpt-image-2.5-sunburst","gpt-image-2.5-sunburst","2026-09-08",G,12,"OpenAI","image"),
 m("gpt-6-luna","gpt-6-luna","2026-09-22",G,15000),
 m("gpt-realtime-2.1-mini","gpt-realtime-2.1-mini","2026-07-07",G,15,"OpenAI","audio"),
 m("gpt-image-2.5-flare","gpt-image-2.5-flare","2026-09-08",G,12,"OpenAI","image"),
 m("gpt-transcribe","gpt-transcribe","2026-07-28",G,15,"OpenAI","audio"),
 m("gpt-live-transcribe","gpt-live-transcribe","2026-07-28",G,15,"OpenAI","audio"),
 m("gpt-realtime-2.1","gpt-realtime-2.1","2026-07-07",G,15,"OpenAI","audio"),
 m("gpt-live-1","gpt-live-1","2026-09-10",G,15,"OpenAI","audio"),
 m("gpt-4.1","gpt-4-1-global","2025-04-14",G,134950), m("gpt-4o","gpt-4o-global","2024-11-20",G,133699),
 m("gpt-chat-latest","gpt-chat-latest","2026-08-06",G,4000)]
def same(n, v, c, f="OpenAI", k="chat", s=G): return m(n, n, v, s, c, f, k)
chat = [same("gpt-5.4-mini","2026-03-17",10), same("gpt-5.4-nano","2026-03-17",10),
 same("gpt-5.1-codex-mini","2025-11-13",10), same("gpt-6.1-terra","2026-10-26",10),
 same("gpt-oss-120b","1",500,"OpenAI-OSS"), same("Llama-3.3-70B-Instruct","9",1000,"Meta"),
 same("Phi-4","7",1000,"Microsoft"), same("Ministral-3B","1",1000,"Mistral AI"),
 same("Mistral-Large-3","1",5000,"Mistral AI"), same("Mistral-large","1",1,"Mistral AI"),
 same("mistral-medium-2505","1",1000,"Mistral AI"), same("mistral-small-2503","1",1000,"Mistral AI"),
 same("Codestral-2501","2",1000,"Mistral AI"), same("cohere-command-a","1",100,"Cohere"),
 same("grok-4-1-fast-non-reasoning","1",10,"xAI"), same("grok-code-fast-1","1",10,"xAI")]
chat[3]["version"] = "2026-10-26"
claude = [same("claude-haiku-4-5","2",10,"Anthropic"), same("claude-haiku-5-5","2",10,"Anthropic"),
 same("claude-sonnet-4-6","1",10,"Anthropic"), same("claude-sonnet-5","2",10,"Anthropic"),
 same("claude-sonnet-5-5","2",10,"Anthropic")]
emb = [same("text-embedding-3-large","1",120,k="embedding"), same("text-embedding-3-small","1",120,k="embedding"),
 same("text-embedding-ada-002","2",120,k="embedding"), same("embed-v-4-0","1",15000,"Cohere","embedding"),
 same("Cohere-embed-v3-english","1",1,"Cohere","embedding"), same("Cohere-embed-v3-multilingual","1",1,"Cohere","embedding"),
 same("Cohere-rerank-v4.0-fast","1",500,"Cohere","embedding"), same("Cohere-rerank-v4.0-pro","1",500,"Cohere","embedding")]
img = [same("MAI-Image-2.6","2026-07-31",1,"Microsoft","image"), same("MAI-Image-2.6-Flash","2026-07-31",10,"Microsoft","image")]
reas = [same("gpt-5.4","2026-03-05",10), same("gpt-5.4-pro","2026-03-05",10), same("gpt-5-pro","2025-10-06",10),
 same("gpt-5-codex","2025-09-15",10), same("gpt-5.1-codex","2025-11-13",10), same("gpt-6.1-sol","2026-09-29",10),
 same("grok-4","1",10,"xAI"), same("grok-4-1-fast-reasoning","1",10,"xAI"), same("grok-4.7","1",10,"xAI"),
 same("DeepSeek-V3.2","1",5000,"DeepSeek"), same("DeepSeek-V3.2-Speciale","1",5000,"DeepSeek"),
 same("DeepSeek-V4-Flash","2026-04-23",5000,"DeepSeek"), same("DeepSeek-V4-Pro","2026-04-23",5000,"DeepSeek"),
 same("Phi-4-mini-reasoning","1",1000,"Microsoft")]
opus = [same("claude-opus-4-5","20251101",10,"Anthropic"), same("claude-opus-4-6","1",10,"Anthropic"),
 same("claude-opus-4-7","1",10,"Anthropic"), same("claude-opus-4-8","2",10,"Anthropic"),
 same("claude-opus-5","2",10,"Anthropic"), same("claude-opus-5-5","2",10,"Anthropic"),
 same("claude-opus-5-5-fast","2",10,"Anthropic")]
mm = [same("Phi-4-multimodal-instruct","1",1000,"Microsoft"),
 same("Llama-4-Maverick-17B-128E-Instruct-FP8","1",10,"Meta"), same("Llama-4-Scout-17B-16E-Instruct","1",1000,"Meta")]
sp = [same("gpt-4o-mini-transcribe","2025-12-15",60,k="audio"), same("gpt-4o-transcribe","2025-03-20",50,k="audio",s=D),
 same("gpt-4o-transcribe-diarize","2025-10-15",50,k="audio"), same("gpt-audio-1.5","2026-02-23",100,k="audio"),
 same("gpt-realtime","2025-08-28",10,k="audio"), same("gpt-realtime-1.5","2026-02-23",10,k="audio"),
 same("gpt-realtime-mini","2025-12-15",30,k="audio"), same("gpt-realtime-translate","2026-05-06",15,k="audio"),
 same("gpt-realtime-whisper","2026-05-06",15,k="audio")]
img2 = [same("gpt-image-1-mini","2025-10-06",1,k="image"), same("gpt-image-1.5","2025-12-16",3,k="image"),
 same("FLUX-1.1-pro","1",1,"Black Forest Labs","image"), same("FLUX.1-Kontext-pro","1",1,"Black Forest Labs","image"),
 same("FLUX.2-pro","1",1,"Black Forest Labs","image"), same("FLUX.2-flex","1",1,"Black Forest Labs","image")]
def ptu(n, d, v, c, f): return m(n, d, v, P, c, f)
od1 = [ptu("gpt-5.4","gpt-5.4-ptu","2026-03-05",15,"OpenAI"), ptu("gpt-6.1-sol","gpt-6.1-sol-ptu","2026-09-29",15,"OpenAI"),
 ptu("gpt-5.1-codex","gpt-5.1-codex-ptu","2025-11-13",15,"OpenAI"), ptu("DeepSeek-V3.2","DeepSeek-V3.2-ptu","1",100,"DeepSeek")]
od2 = [ptu("gpt-5.4-mini","gpt-5.4-mini-ptu","2026-03-17",15,"OpenAI"), ptu("Llama-3.3-70B-Instruct","Llama-3.3-70B-Instruct-ptu","9",15,"Meta")]
groups = [
 grp("legacy-werc",LEGACY_RG,"eastus2",LEGACY_ACCOUNT,"other",legacy),
 grp("chat-eus","rg-ccp-chat-eus","eastus",f"{PREFIX}-chat-eus-aoai","chat",chat),
 grp("claude-eus","rg-ccp-claude-eus","eastus",f"{PREFIX}-claude-eus-aoai","chat",claude),
 grp("embeddings-eus","rg-ccp-embeddings-eus","eastus",f"{PREFIX}-embeddings-eus-aoai","embeddings",emb),
 grp("image-eus","rg-ccp-image-eus","eastus",f"{PREFIX}-image-eus-aoai","image",img),
 grp("reasoning-eus2","rg-ccp-reasoning-eus2","eastus2",f"{PREFIX}-reasoning-eus2-aoai","reasoning",reas),
 grp("claude-opus-eus2","rg-ccp-claude-opus-eus2","eastus2",f"{PREFIX}-claude-opus-eus2-aoai","reasoning",opus),
 grp("multimodal-eus2","rg-ccp-multimodal-eus2","eastus2",f"{PREFIX}-multimodal-eus2-aoai","multimodal",mm),
 grp("speech-eus2","rg-ccp-speech-eus2","eastus2",f"{PREFIX}-speech-eus2-aoai","speech",sp),
 grp("image-eus2","rg-ccp-image-eus2","eastus2",f"{PREFIX}-image-eus2-aoai","image",img2),
 grp("reasoning-eus2-od","rg-ccp-reasoning-eus2-od","eastus2",f"{PREFIX}-reasoning-eus2-od-aoai","reasoning",od1,"on_demand","idle_cost"),
 grp("chat-eus-od","rg-ccp-chat-eus-od","eastus",f"{PREFIX}-chat-eus-od-aoai","chat",od2,"on_demand","idle_cost")]
for g in groups:
    if g["billing"] == "idle_cost": assert g["policy"] == "on_demand"
json.dump({"version": 1, "groups": groups}, open(sys.argv[1] if len(sys.argv) > 1 else "ccp_groups.seed.json", "w"), indent=1)
