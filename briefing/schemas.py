"""JSON schemas for structured LLM output."""

STR = {"type": "string"}
NUM = {"type": "number"}
STRS = {"type": "array", "items": STR}

CLUSTER = {
    "type": "object",
    "properties": {"stories": {"type": "array", "items": {
        "type": "object",
        "properties": {"headline": STR, "items": STRS},
        "required": ["headline", "items"],
    }}},
    "required": ["stories"],
}

SCORE = {
    "type": "object",
    "properties": {"scores": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "story": STR, "impact": NUM, "novelty": NUM, "breadth": NUM, "signal": NUM,
            "kind": {"type": "string", "enum": ["launch", "release", "research", "policy", "security",
                                                 "customer_story", "opinion", "other"]},
            "rationale": STR,
        },
        "required": ["story", "impact", "novelty", "breadth", "signal", "kind", "rationale"],
    }}},
    "required": ["scores"],
}

THREAD = {
    "type": "object",
    "properties": {"thread": STR, "stories": STRS},  # empty string = no theme
    "required": ["thread", "stories"],
}

ANALYSIS = {
    "type": "object",
    "properties": {
        "what_happened": STR, "why_it_matters": STR, "contested": {"type": "boolean"},
        "case_for": STR, "case_against": STR, "so_what": STR,
        "recommendation": {"type": "string", "enum": ["evaluate", "pilot", "ignore"]},
        "recommendation_reason": STR,
        "key_facts": {"type": "array", "items": {
            "type": "object", "properties": {"fact": STR, "source_url": STR}, "required": ["fact", "source_url"],
        }},
    },
    "required": ["what_happened", "why_it_matters", "contested", "case_for", "case_against",
                 "so_what", "recommendation", "recommendation_reason", "key_facts"],
}

LINES = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "speaker": {"type": "string", "enum": ["lead", "counterpoint"]},
            "text": STR,
            "refs": STRS,
        },
        "required": ["speaker", "text"],
    }}},
    "required": ["lines"],
}

FACTCHECK = {
    "type": "object",
    "properties": {"issues": {"type": "array", "items": {
        "type": "object",
        "properties": {"line": {"type": "integer"}, "problem": STR, "fix": STR},
        "required": ["line", "problem", "fix"],
    }}},
    "required": ["issues"],
}
