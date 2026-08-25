"""Skill extraction against a curated taxonomy.

Why a taxonomy instead of "just take the tags": tags only exist on two of the
four sources, they are inconsistent ("reactjs" vs "React.js" vs "react"), and
they miss everything stated only in prose. A controlled vocabulary with aliases
gives one canonical slug per technology, which is what makes cross-source
aggregation meaningful.

The hard part is ambiguity. "Go", "R", "C" and "Rust" are real languages and
also ordinary English. Those are marked ``strict`` and only match with an
explicit qualifier ("Golang", "Go developer", "R programming"), trading a little
recall for precision - the right trade when the output is a demand ranking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

MAX_SCAN_CHARS = 14_000


@dataclass(frozen=True, slots=True)
class SkillDef:
    slug: str
    name: str
    category: str
    aliases: tuple[str, ...] = ()
    strict_patterns: tuple[str, ...] = ()  # set for ambiguous names
    probes: tuple[str, ...] = ()  # literal pre-filter; defaults to name + aliases


@dataclass(slots=True)
class SkillMatch:
    slug: str
    name: str
    category: str
    matched_via: str
    confidence: float = 1.0


def _s(slug: str, name: str, category: str, *aliases: str) -> SkillDef:
    return SkillDef(slug=slug, name=name, category=category, aliases=aliases)


TAXONOMY: tuple[SkillDef, ...] = (
    # --- languages ----------------------------------------------------
    _s("python", "Python", "language", "python3", "python 3"),
    _s("javascript", "JavaScript", "language", "js", "ecmascript", "es6"),
    _s("typescript", "TypeScript", "language", "ts"),
    _s("java", "Java", "language", "java 17", "java8", "java 8"),
    _s("csharp", "C#", "language", "c sharp", "c-sharp", "dotnet c#"),
    _s("cpp", "C++", "language", "cplusplus", "c plus plus"),
    _s("php", "PHP", "language"),
    _s("ruby", "Ruby", "language"),
    _s("scala", "Scala", "language"),
    _s("kotlin", "Kotlin", "language"),
    _s("swift", "Swift", "language", "swiftui"),
    _s("perl", "Perl", "language"),
    _s("haskell", "Haskell", "language"),
    _s("elixir", "Elixir", "language"),
    _s("erlang", "Erlang", "language"),
    _s("clojure", "Clojure", "language"),
    _s("lua", "Lua", "language"),
    _s("dart", "Dart", "language"),
    _s("solidity", "Solidity", "language"),
    _s("sql", "SQL", "language"),
    _s("bash", "Bash/Shell", "language", "shell scripting", "zsh", "shell script"),
    SkillDef("golang", "Go", "language", ("golang",),
             (r"\bgolang\b", r"\bgo\s+(?:developer|engineer|programmer|programming|lang|services|microservices|codebase)\b",
              r"\b(?:written|build|building|writing|experience)\s+in\s+go\b")),
    SkillDef("rlang", "R", "language", (),
             (r"\br\s+programming\b", r"\br\s+language\b", r"\b(?:python|sas|stata|matlab)\s*(?:,|/|and|or)\s*r\b",
              r"\br\s*(?:,|/)\s*(?:python|sas|stata|matlab)\b"),
             ("r programming", "r language", "python", "sas", "stata", "matlab")),
    SkillDef("clang", "C", "language", (),
             (r"\bc\s*/\s*c\+\+", r"\bc\s+programming\b", r"\bembedded\s+c\b", r"\bansi\s+c\b"),
             ("c/c", "c /", "c programming", "embedded c", "ansi c")),
    SkillDef("rust", "Rust", "language", (),
             (r"\brust\b(?![-\s]*(?:proof|belt|ed\b|ing\b|y\b))",)),
    # --- frontend -----------------------------------------------------
    _s("react", "React", "frontend", "react.js", "reactjs", "react js"),
    _s("nextjs", "Next.js", "frontend", "nextjs", "next js"),
    _s("vue", "Vue.js", "frontend", "vuejs", "vue.js", "vue 3", "nuxt"),
    _s("angular", "Angular", "frontend", "angularjs", "angular.js"),
    _s("svelte", "Svelte", "frontend", "sveltekit"),
    _s("html", "HTML", "frontend", "html5"),
    _s("css", "CSS", "frontend", "css3"),
    _s("tailwind", "Tailwind CSS", "frontend", "tailwindcss"),
    _s("sass", "Sass/SCSS", "frontend", "scss"),
    _s("redux", "Redux", "frontend"),
    _s("webpack", "Webpack", "frontend"),
    _s("vite", "Vite", "frontend"),
    _s("jquery", "jQuery", "frontend"),
    _s("webgl", "WebGL", "frontend", "three.js", "threejs"),
    # A bare "screen reader" match picks up the accessibility notice attached to
    # application forms, which is boilerplate rather than a job requirement.
    SkillDef("accessibility", "Accessibility", "frontend", (),
             (r"\ba11y\b", r"\bwcag\b", r"\bsection 508\b",
              r"\b(?:web|digital|product|design)\s+accessib\w+",
              r"\baccessib\w+\s+(?:standards|guidelines|best practices|requirements|testing|audits?|compliance)\b",
              r"\bscreen reader\s+(?:support|testing|compatibility)\b"),
             ("a11y", "wcag", "section 508", "accessib", "screen reader")),
    # --- backend / frameworks -----------------------------------------
    _s("nodejs", "Node.js", "backend", "node.js", "nodejs", "node js"),
    _s("django", "Django", "backend"),
    _s("flask", "Flask", "backend"),
    _s("fastapi", "FastAPI", "backend"),
    _s("rails", "Ruby on Rails", "backend", "ruby on rails", "rails"),
    _s("spring", "Spring", "backend", "spring boot", "springboot"),
    _s("dotnet", ".NET", "backend", "asp.net", "dotnet", ".net core", "asp net"),
    _s("laravel", "Laravel", "backend"),
    _s("express", "Express.js", "backend", "expressjs", "express.js"),
    _s("nestjs", "NestJS", "backend", "nest.js"),
    _s("graphql", "GraphQL", "backend", "apollo graphql"),
    _s("rest", "REST APIs", "backend", "restful", "rest api", "rest apis"),
    _s("grpc", "gRPC", "backend", "protobuf", "protocol buffers"),
    _s("websockets", "WebSockets", "backend", "websocket"),
    _s("microservices", "Microservices", "backend", "micro-services"),
    # --- data stores ---------------------------------------------------
    _s("postgresql", "PostgreSQL", "database", "postgres", "psql"),
    _s("mysql", "MySQL", "database", "mariadb"),
    _s("mongodb", "MongoDB", "database", "mongo"),
    _s("redis", "Redis", "database"),
    _s("elasticsearch", "Elasticsearch", "database", "elastic search", "opensearch"),
    _s("cassandra", "Cassandra", "database", "scylladb"),
    _s("dynamodb", "DynamoDB", "database", "dynamo db"),
    _s("sqlite", "SQLite", "database"),
    _s("snowflake", "Snowflake", "database"),
    _s("bigquery", "BigQuery", "database", "big query"),
    _s("redshift", "Redshift", "database"),
    _s("clickhouse", "ClickHouse", "database"),
    _s("neo4j", "Neo4j", "database", "graph database"),
    _s("duckdb", "DuckDB", "database"),
    # --- cloud / infra -------------------------------------------------
    _s("aws", "AWS", "cloud", "amazon web services", "ec2", "s3", "lambda"),
    _s("gcp", "Google Cloud", "cloud", "google cloud platform", "gcp"),
    _s("azure", "Azure", "cloud", "microsoft azure"),
    _s("docker", "Docker", "devops", "containerization", "containers"),
    _s("kubernetes", "Kubernetes", "devops", "k8s", "eks", "gke"),
    _s("terraform", "Terraform", "devops", "hashicorp terraform"),
    _s("ansible", "Ansible", "devops"),
    _s("jenkins", "Jenkins", "devops"),
    _s("githubactions", "GitHub Actions", "devops", "github action"),
    _s("gitlabci", "GitLab CI", "devops", "gitlab ci/cd"),
    _s("cicd", "CI/CD", "devops", "ci/cd", "continuous integration", "continuous delivery"),
    _s("linux", "Linux", "devops", "unix", "ubuntu", "debian"),
    _s("nginx", "Nginx", "devops"),
    _s("kafka", "Kafka", "devops", "apache kafka"),
    _s("rabbitmq", "RabbitMQ", "devops"),
    _s("prometheus", "Prometheus", "devops", "grafana"),
    _s("datadog", "Datadog", "devops"),
    _s("observability", "Observability", "devops", "opentelemetry", "distributed tracing"),
    _s("serverless", "Serverless", "cloud", "aws lambda", "cloud functions"),
    _s("cloudflare", "Cloudflare", "cloud"),
    # --- data / ML ------------------------------------------------------
    _s("pandas", "pandas", "data", "numpy"),
    # "Spark Capital" (an investor) and "spark joy" are not the data engine.
    SkillDef("spark", "Apache Spark", "data", (),
             (r"\bapache spark\b", r"\bpyspark\b",
              r"\bspark\s+(?:sql|streaming|jobs?|clusters?|pipelines?|applications?)\b",
              r"\b(?:scala|hadoop|databricks|airflow|hive|emr|flink|etl)\b[^.]{0,60}\bspark\b",
              r"\bspark\b[^.]{0,60}\b(?:scala|hadoop|databricks|airflow|hive|emr|flink|etl)\b"),
             ("spark",)),
    _s("airflow", "Airflow", "data", "apache airflow"),
    _s("dbt", "dbt", "data", "data build tool"),
    _s("etl", "ETL/ELT", "data", "elt", "data pipeline", "data pipelines"),
    _s("datawarehouse", "Data Warehousing", "data", "data warehouse", "dimensional modeling"),
    _s("tableau", "Tableau", "data"),
    _s("powerbi", "Power BI", "data", "powerbi"),
    _s("looker", "Looker", "data"),
    _s("pytorch", "PyTorch", "ml", "torch"),
    _s("tensorflow", "TensorFlow", "ml", "keras"),
    _s("scikitlearn", "scikit-learn", "ml", "sklearn", "scikit learn"),
    _s("llm", "LLMs", "ml", "large language model", "large language models", "gpt-4", "openai api"),
    _s("rag", "RAG", "ml", "retrieval augmented generation", "retrieval-augmented"),
    _s("nlp", "NLP", "ml", "natural language processing"),
    _s("computervision", "Computer Vision", "ml", "opencv", "image recognition"),
    _s("mlops", "MLOps", "ml", "ml ops", "model deployment", "mlflow"),
    _s("vectordb", "Vector Databases", "ml", "pinecone", "weaviate", "pgvector", "qdrant", "faiss"),
    _s("langchain", "LangChain", "ml", "llamaindex"),
    _s("statistics", "Statistics", "data", "statistical modeling", "statistical analysis"),
    _s("ab_testing", "A/B Testing", "data", "a/b test", "experimentation"),
    # --- mobile ---------------------------------------------------------
    _s("ios", "iOS", "mobile", "ios development"),
    _s("android", "Android", "mobile", "android development"),
    _s("reactnative", "React Native", "mobile", "react-native"),
    _s("flutter", "Flutter", "mobile"),
    # --- testing / quality ----------------------------------------------
    _s("pytest", "pytest", "testing"),
    _s("jest", "Jest", "testing"),
    _s("cypress", "Cypress", "testing"),
    _s("playwright", "Playwright", "testing"),
    _s("selenium", "Selenium", "testing"),
    _s("tdd", "TDD", "testing", "test driven development", "test-driven"),
    _s("unittesting", "Unit Testing", "testing", "unit tests"),
    # --- practices / tooling ---------------------------------------------
    _s("git", "Git", "tool", "github", "gitlab", "version control"),
    _s("agile", "Agile", "practice", "scrum", "kanban", "sprint planning"),
    _s("systemdesign", "System Design", "practice", "distributed systems", "system architecture"),
    # "Security" unqualified matches policy boilerplate ("our security and privacy
    # standards") in every posting, which made it the #1 skill in the corpus.
    SkillDef("security", "Security", "practice", (),
             (r"\b(?:application|information|cyber|cloud|network|product|data|infrastructure)\s+security\b",
              r"\bsecurity\s+(?:engineer|engineering|analyst|architect|researcher|operations|posture|review|audit|controls|testing|hardening|practices|clearance)\b",
              r"\b(?:appsec|infosec|owasp|penetration testing|pen testing|threat model)",
              r"\bsecure\s+(?:coding|by design|software development)\b"),
             ("security", "appsec", "infosec", "owasp", "penetration testing",
              "pen testing", "threat model", "secure coding")),
    _s("scraping", "Web Scraping", "practice", "web scraping", "crawler", "data extraction"),
    _s("jira", "Jira", "tool", "confluence"),
    _s("figma", "Figma", "tool"),
    # "excel" is also a verb - "candidates who excel at client acquisition".
    SkillDef("excel", "Excel", "tool", (),
             (r"\b(?:microsoft|ms)\s+excel\b",
              r"\bexcel\s+(?:skills|spreadsheets?|models?|modelling|modeling|proficiency|formulas|macros|vba|pivot)\b",
              r"\b(?:advanced|strong|proficient|proficiency|expert|intermediate)\s+(?:in\s+|with\s+)?excel\b",
              r"\b(?:in|using|with)\s+excel\b",
              r"\bexcel\s*[,/)]",
              r"\bspreadsheets?\b"),
             ("excel", "spreadsheet")),
    _s("salesforce", "Salesforce", "tool", "crm"),
    _s("hubspot", "HubSpot", "tool"),
    _s("seo", "SEO", "practice", "search engine optimization"),
)

SKILLS_BY_SLUG: dict[str, SkillDef] = {skill.slug: skill for skill in TAXONOMY}


def _skill_pattern(skill: SkillDef) -> str:
    if skill.strict_patterns:
        return "|".join(skill.strict_patterns)
    terms = sorted({skill.name.lower(), *skill.aliases}, key=len, reverse=True)
    # Custom boundaries so "C++", "C#", ".NET" and "Node.js" match, while
    # "java" never matches inside "javascript".
    return "|".join(rf"(?<![\w+#.]){re.escape(term)}(?![\w+#])" for term in terms)


@lru_cache(maxsize=1)
def _matchers() -> tuple[tuple[SkillDef, re.Pattern[str], tuple[str, ...]], ...]:
    """Compile a two-stage matcher per skill: cheap literal probe, then regex.

    Running ~130 regexes against every 14 KB description is the slowest thing in
    the pipeline. Almost all of them cannot possibly match, and ``substring in
    text`` is a C-speed scan, so the probe rejects the vast majority before any
    regex engine work happens. Only skills whose literal actually appears pay for
    a real match.
    """
    matchers = []
    for skill in TAXONOMY:
        probes = skill.probes or tuple({skill.name.lower(), *(a.lower() for a in skill.aliases)})
        matchers.append((skill, re.compile(_skill_pattern(skill), re.IGNORECASE), probes))
    return tuple(matchers)


@lru_cache(maxsize=1)
def _alias_index() -> dict[str, SkillDef]:
    """Exact-match lookup used for source-provided tags."""
    index: dict[str, SkillDef] = {}
    for skill in TAXONOMY:
        for term in (skill.slug, skill.name, *skill.aliases):
            index.setdefault(term.lower().strip(), skill)
    return index


def extract_skills(
    description: str | None,
    title: str | None = None,
    tags: list[str] | None = None,
    company: str | None = None,
) -> list[SkillMatch]:
    """Return the skills mentioned, best evidence per skill.

    Confidence reflects where the evidence came from: a source-curated tag is
    stronger than a passing mention in a 5,000 word description.

    ``company`` suppresses employer self-mentions. Without it, scraping Figma's
    own board makes "Figma" the most in-demand skill on the board, because every
    Figma posting talks about Figma - a real skew we hit on the first crawl.
    """
    found: dict[str, SkillMatch] = {}
    company_slug = _company_skill_slug(company)

    def record(skill: SkillDef, via: str, confidence: float) -> None:
        if skill.slug == company_slug:
            return
        existing = found.get(skill.slug)
        if existing is None or confidence > existing.confidence:
            found[skill.slug] = SkillMatch(
                slug=skill.slug, name=skill.name, category=skill.category,
                matched_via=via, confidence=confidence,
            )

    index = _alias_index()
    for tag in tags or []:
        skill = index.get(tag.lower().strip())
        if skill is not None:
            record(skill, "tag", 1.0)

    haystacks: list[tuple[str, str, float]] = []
    if title:
        haystacks.append((title, "title", 0.95))
    if description:
        haystacks.append((description[:MAX_SCAN_CHARS], "description", 0.8))

    for text, via, confidence in haystacks:
        lowered = text.lower()
        for skill, pattern, probes in _matchers():
            if not any(probe in lowered for probe in probes):
                continue
            if pattern.search(text):
                record(skill, via, confidence)

    return sorted(found.values(), key=lambda m: (-m.confidence, m.slug))


def _company_skill_slug(company: str | None) -> str | None:
    """Map an employer name onto a taxonomy slug, when it is one (Figma, Docker...)."""
    if not company:
        return None
    definition = _alias_index().get(company.strip().lower())
    return definition.slug if definition else None
