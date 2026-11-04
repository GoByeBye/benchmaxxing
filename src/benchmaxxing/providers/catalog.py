"""Reviewed upstream releases; counts describe sources, never installed coverage."""

from benchmaxxing.providers.contracts import SuiteDescriptor


def _suite(
    id: str,
    name: str,
    category: str,
    url: str,
    version: str,
    size: str,
    description: str,
    license: str,
    offline: str,
    requirements: list[str],
) -> SuiteDescriptor:
    return SuiteDescriptor.model_validate(
        dict(
            id=id,
            name=name,
            category=category,
            upstream_url=url,
            version=version,
            published_size=size,
            description=description,
            license=license,
            offline=offline,
            requirements=requirements,
        )
    )


SUITES = [
    _suite(
        "terminal-bench-2-1",
        "Terminal-Bench 2.1",
        "terminal",
        "https://www.tbench.ai/news/terminal-bench-2-1",
        "2.1",
        "89 tasks",
        "Multi-step terminal, build, debugging and data workflows.",
        "Apache-2.0; asset notices",
        "adaptation",
        ["Per-task Linux images", "Offline dependency audit", "Native verifier"],
    ),
    _suite(
        "terminal-bench-4",
        "Terminal-Bench 4.0",
        "terminal",
        "https://www.tbench.ai/news/terminal-bench-4-0",
        "4.0.0",
        "66 tasks",
        "Long-horizon terminal work; upstream permits internet and eight-hour tasks.",
        "Apache-2.0; asset notices",
        "conditional",
        ["Offline variant required", "Some tasks require large GPUs", "Per-task Linux images"],
    ),
    _suite(
        "miniwob",
        "MiniWoB++",
        "browser",
        "https://github.com/Farama-Foundation/miniwob-plusplus",
        "frozen source revision",
        "125 templates",
        "Seeded browser interactions on local HTML.",
        "MIT; BrowserGym Apache-2.0",
        "supported-path",
        ["BrowserGym", "Prepared Chromium"],
    ),
    _suite(
        "webarena-verified",
        "WebArena-Verified",
        "browser",
        "https://github.com/ServiceNow/webarena-verified",
        "frozen source revision",
        "812 tasks",
        "Realistic website workflows with deterministic replay grading.",
        "Apache-2.0; website/data licenses",
        "adaptation",
        ["Prepared local websites and databases", "State reset", "BrowserGym", "HAR collection"],
    ),
    _suite(
        "visualwebarena",
        "VisualWebArena",
        "browser",
        "https://github.com/web-arena-x/visualwebarena",
        "frozen source revision",
        "910 tasks",
        "Browser tasks requiring visual understanding.",
        "MIT; website/data licenses",
        "conditional",
        ["Prepared local websites", "Vision-capable model", "Local grading models"],
    ),
    _suite(
        "evalplus-humaneval",
        "EvalPlus · HumanEval+",
        "programming",
        "https://github.com/evalplus/evalplus",
        "frozen dataset revision",
        "164 tasks",
        "Python function synthesis with expanded correctness checks.",
        "Apache-2.0",
        "supported-path",
        ["EvalPlus grading image"],
    ),
    _suite(
        "evalplus-mbpp",
        "EvalPlus · MBPP+",
        "programming",
        "https://github.com/evalplus/evalplus",
        "frozen dataset revision",
        "378 tasks",
        "Python programming problems with expanded differential tests.",
        "Apache-2.0",
        "supported-path",
        ["EvalPlus grading image"],
    ),
    _suite(
        "bigcodebench",
        "BigCodeBench",
        "programming",
        "https://github.com/bigcode-project/bigcodebench",
        "frozen dataset revision",
        "1,140 tasks",
        "Practical, library-heavy Python generation; includes a 148-task Hard subset.",
        "Apache-2.0",
        "supported-path",
        ["Prepared Python library image", "Local native grader"],
    ),
    _suite(
        "swebench-lite",
        "SWE-bench Lite",
        "programming",
        "https://github.com/SWE-bench/SWE-bench",
        "frozen dataset revision",
        "300 tasks",
        "Fix real issues in repository snapshots.",
        "MIT harness; target repository licenses",
        "supported-path",
        ["Per-instance repository images", "Official patch and log grading"],
    ),
    _suite(
        "swebench-verified",
        "SWE-bench Verified",
        "programming",
        "https://github.com/SWE-bench/SWE-bench",
        "frozen dataset revision",
        "500 tasks",
        "Engineer-validated repository issue repair.",
        "MIT harness; target repository licenses",
        "supported-path",
        ["Per-instance repository images", "Official patch and log grading"],
    ),
    _suite(
        "scicode-verified",
        "SciCode-Verified",
        "engineering",
        "https://github.com/flyingwagner/scicode-verified",
        "v2",
        "64 main problems / 287 subproblems",
        "Scientific code generation with corrected frozen evaluation targets.",
        "Apache-2.0",
        "supported-path",
        ["Scientific Python runtime", "HDF5 grading targets", "Declared evaluation variant"],
    ),
    _suite(
        "intercode-ctf",
        "InterCode-CTF",
        "security",
        "https://github.com/princeton-nlp/intercode",
        "frozen source revision",
        "100 tasks",
        "Bash-driven security challenges and flag submission.",
        "MIT harness; picoCTF asset terms",
        "adaptation",
        ["Prepared CTF assets", "Offline task audit"],
    ),
    _suite(
        "cybench",
        "Cybench",
        "security",
        "https://github.com/andyzorigin/cybench",
        "frozen source revision",
        "40 tasks",
        "CTF challenges with local services and optional subtasks.",
        "Apache-2.0; competition asset notices",
        "adaptation",
        ["Task-local service networks", "Unprivileged environment adaptation"],
    ),
    _suite(
        "cybergym",
        "CyberGym",
        "security",
        "https://github.com/sunblaze-ucb/cybergym",
        "frozen dataset revision",
        "1,507 vulnerabilities",
        "Generate reproducers for real vulnerabilities across 188 projects.",
        "Apache-2.0; target project licenses",
        "conditional",
        [
            "Full source assets exceed the 250 GB preparation budget",
            "Prepared evaluation server images",
        ],
    ),
    _suite(
        "xstest",
        "XSTest",
        "refusal",
        "https://github.com/paul-rottger/xstest",
        "NAACL-2024",
        "450 prompts",
        "Over-refusal and unsafe-request refusal are reported separately.",
        "CC-BY-4.0 prompts",
        "supported-path",
        ["Human assessment or separately identified local judge"],
    ),
    _suite(
        "harmbench",
        "HarmBench",
        "refusal",
        "https://github.com/centerforaisafety/HarmBench",
        "1.0",
        "510 behaviors",
        "Robust refusal and red-team behavior evaluation.",
        "MIT; classifier model terms",
        "conditional",
        ["Prepared local classifier", "Separate text/multimodal evaluation profiles"],
    ),
    _suite(
        "workarena",
        "WorkArena",
        "browser",
        "https://github.com/ServiceNow/WorkArena",
        "L1 / ++",
        "33 atomic types / 682 compositional tasks",
        "Enterprise browser workflows on hosted ServiceNow instances.",
        "Apache-2.0 code; gated hosted-instance terms",
        "unavailable",
        ["Requires hosted ServiceNow access; unavailable in fully offline mode"],
    ),
]

SUITES.extend(
    [
        SuiteDescriptor(
            id="frontend-design",
            name="Benchmaxxing Frontend Design",
            category="frontend",
            upstream_url="https://github.com/GoByeBye/benchmaxxing",
            version="1.0.0",
            published_size="6 original tasks",
            description="Build local interfaces with browser-verified behavior, accessibility, "
            "responsive layouts and separate visual review artifacts.",
            license="AGPL-3.0-only",
            offline="supported-path",
            origin="original",
            requirements=["Prepared local Chromium runtime", "Separate human aesthetic review"],
        ),
        SuiteDescriptor(
            id="frontend-design-extended",
            name="Benchmaxxing Frontend Design Extended",
            category="frontend",
            upstream_url="https://github.com/GoByeBye/benchmaxxing",
            version="1.0.0",
            published_size="3 original tasks",
            description="Additional offline interface briefs with browser-verified behavior, "
            "responsive layouts and separate visual review artifacts.",
            license="AGPL-3.0-only",
            offline="supported-path",
            origin="original",
            requirements=["Prepared local Chromium runtime", "Separate human aesthetic review"],
        ),
        SuiteDescriptor(
            id="cyberlabs",
            name="Benchmaxxing Application Security Labs",
            category="security",
            upstream_url="https://github.com/GoByeBye/benchmaxxing",
            version="1.0.0",
            published_size="Original application security labs",
            description="Exploit and repair deliberately vulnerable local applications with "
            "fresh isolated targets and independent verification.",
            license="AGPL-3.0-only",
            offline="supported-path",
            origin="original",
            requirements=[
                "Prepared Linux target and candidate images",
                "Isolated task-local network",
            ],
            active=False,
        ),
    ]
)


def get_suite(suite_id: str) -> SuiteDescriptor:
    for suite in SUITES:
        if suite.id == suite_id:
            return suite
    raise ValueError(f"Unknown benchmark suite: {suite_id}")


def require_active_suite(suite_id: str) -> SuiteDescriptor:
    suite = get_suite(suite_id)
    if not suite.active:
        raise ValueError(
            f"{suite.name} is archived and outside the active benchmark scope. "
            "Existing task records, runs and assets remain available for inspection."
        )
    return suite
