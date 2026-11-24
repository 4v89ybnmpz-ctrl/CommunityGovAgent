"""规范性检查：模板解析。把 YAML 和 Markdown 模板收成统一字段。"""

from copy import deepcopy
import math
import re
from pathlib import PurePosixPath
from typing import Any

from markdown_it import MarkdownIt
import yaml

class TemplateLoader(yaml.SafeLoader):
    """保留 Yes/No/On/Off 等选项文字，仅将 true/false 当作布尔值。"""


TemplateLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in rules if tag != 'tag:yaml.org,2002:bool']
    for key, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
TemplateLoader.add_implicit_resolver(
    'tag:yaml.org,2002:bool', re.compile(r'^(?:true|false)$', re.IGNORECASE), list('tTfF'))


def _label(text: str) -> str:
    """提取 Markdown 的显示文字，用于匹配标题和复选项。

    Args:
        text: 含强调、链接或行内代码的文字。
    Returns:
        去除格式标记及首尾空白的显示文字。
    """
    tokens = MarkdownIt("commonmark").parseInline(text)
    return "".join(token.content for item in tokens for token in (item.children or [])
                   if token.type in {"text", "code_inline", "image"}).strip()


def _section_key(text: str) -> str:
    """生成章节匹配键，忽略空白、大小写、末尾冒号及必填／选填提示。

    仅处理独立提示（含中英双语），保留字段名称中的其他括号内容；
    不用于判断必填规则或匹配复选框选项。
    """
    label = "".join(_label(text).split()).casefold()
    label = label.translate(str.maketrans({"（": "(", "）": ")", "／": "/"}))
    hint = r"(?:必填|必选|选填|可选|required|mandatory|optional)"
    hints = hint + r"(?:[/／|、]" + hint + r")*"
    suffix = rf"(?:\({hints}\)|（{hints}）|\[{hints}\])"
    # 冒号可以位于提示前后；每轮仅移除末尾格式，不删除业务括号。
    while True:
        stripped = re.sub(rf"(?:{suffix})+$", "", label.rstrip(":："))
        if not stripped or stripped == label:
            return label
        label = stripped


def _section_forms(key: str) -> set[str]:
    """提取双语书写形式和斜杠分隔的可选名称，不推断同义词。"""
    def language(part: str) -> str | None:
        chinese = bool(re.search(r"[\u3400-\u9fff]", part))
        english = bool(re.search(r"[a-z]", part))
        # 带限定括号的名称不能靠拆分丢掉限定条件。
        if re.search(r"[()\[\]]", part):
            return None
        if chinese and not english:
            return "zh"
        if english and not chinese:
            return "en"
        return None

    pair = None
    parenthetical = re.fullmatch(r"([^()]+)\(([^()]+)\)", key)
    if parenthetical:
        left, right = parenthetical.groups()
        if {language(left), language(right)} == {"zh", "en"}:
            pair = (left, right)
    else:
        parts = key.split("/")
        languages = [language(part) for part in parts]
        # 先识别两种语言的完整名称，再提取各侧的斜杠选项。
        switches = [i for i in range(1, len(parts)) if languages[i] != languages[i - 1]]
        if None not in languages and set(languages) == {"zh", "en"} and len(switches) == 1:
            cut = switches[0]
            pair = ("/".join(parts[:cut]), "/".join(parts[cut:]))
    forms = {key}
    if pair:
        left, right = pair
        forms.update((left, right, f"{left}/{right}", f"{right}/{left}",
                      f"{left}({right})", f"{right}({left})"))
    # 只拆独立的斜杠选项；括号中的业务限定（如版本(开发/生产)）保持完整。
    forms.update(part for form in tuple(forms) if not re.search(r"[()\[\]]", form)
                 for part in form.split("/") if part)
    return forms


def _resolve_section_name(key: str, field_names: dict[str, str]) -> str | None:
    """任一语言或斜杠选项匹配即可；指向不同字段时不自动匹配。"""
    if key in field_names:
        return field_names[key]
    targets = {field_names[form] for form in _section_forms(key) if form in field_names}
    return next(iter(targets)) if len(targets) == 1 else None


def _sections(
    body: str, *, normalize_hints: bool = False, field_keys: set[str] | None = None,
    field_names: dict[str, str] | None = None,
) -> dict[str, str]:
    """提取 Markdown 章节及内容，忽略代码块和引用中的伪标题。

    Args:
        body: Issue 的原始 Markdown 正文。
        normalize_hints: 匹配 Issue 时忽略章节名末尾的必填／选填提示；模板解析保留原名。
        field_keys: 检查 Issue 时的模板字段键；提供时仅以这些字段划分章节，不按标题层级截断。
        field_names: 章节书写形式到模板字段键的映射，仅用于 Issue 检查。
    Returns:
        忽略空白后的章节标题到填写内容的映射；同名章节合并，子节内容计入父节。
    """
    # 将正文解析为「章节标题 → 填写内容」，代码块和引用中的标题不算章节。
    tokens = MarkdownIt("commonmark").parse(body)
    headings = [
        (i, int(token.tag[1:]), _section_key(tokens[i + 1].content) if normalize_hints
         else "".join(_label(tokens[i + 1].content).split()))
        for i, token in enumerate(tokens)
        if token.type == "heading_open" and token.level == 0
    ]
    if field_names is not None:
        headings = [(start, level, resolved) for start, level, label in headings
                    if (resolved := _resolve_section_name(label, field_names)) is not None]
    if field_keys is not None:
        headings = [heading for heading in headings if heading[2] in field_keys]
    sections: dict[str, list[str]] = {}
    for position, (start, level, label) in enumerate(headings):
        # 模板本身按 Markdown 层级解析；Issue 按模板字段切分，自加小标题不截断字段。
        end = next(
            (i for i, depth, _ in headings[position + 1:]
             if field_keys is not None or depth <= level),
            len(tokens),
        )
        parts = []
        for i in range(start + 3, end):
            token = tokens[i]
            if token.type in {"fence", "code_block"}:
                parts.append(token.content)
            elif token.type in {"inline", "html_block"}:
                # 子标题本身不算填写内容；HTML 注释也不算。
                if tokens[i - 1].type != "heading_open":
                    parts.append(re.sub(r"<!--[\s\S]*?-->", "", token.content))
        sections.setdefault(label, []).append("\n".join(parts).strip())
    return {label: "\n".join(parts).strip() for label, parts in sections.items()}


def _required_hint(*texts: str | None) -> bool:
    """识别字段说明中的明确必填指示。

    Args:
        texts: 字段名称、描述、占位提示或章节说明；允许 None。
    Returns:
        存在必填、必选、必须填写／选择／勾选、required、mandatory、
        must fill/select/check，或名称首尾的单个星号时返回 True；忽略代码块。
    """
    for text in texts:
        if not text:
            continue
        tokens = MarkdownIt("commonmark").parse(text.replace("<!--", "").replace("-->", ""))
        plain = " ".join(_label(token.content) for token in tokens
                         if token.type in {"inline", "html_block"})
        plain = re.sub(r"(?:非|不(?:是|为)?|无需|无须)\s*(?:必填|必选)|"
                       r"\b(?:not\s+(?:a\s+)?required|not\s+mandatory|non[- ](?:required|mandatory)|optional)\b", "", plain,
                       flags=re.IGNORECASE)
        if re.search(r"必填|必选|必须\s*(?:填写|填入|提供|选择|勾选)|"
                     r"\b(?:required|mandatory|must\s+(?:fill|provide|select|check))\b|"
                     r"^[*＊][^*＊\s]|[^*＊][*＊]\s*$", plain, re.IGNORECASE):
            return True
    return False


def _markdown_fields(body: str, *, required: bool | None = None) -> list[dict[str, Any]]:
    """把 Markdown 填写章节转成文本字段，默认必填。

    Args:
        body: Markdown 模板正文，或文本框中预置的 Markdown。
        required: 外层文本框的必填配置；False 使无明确标记的章节选填。
    Returns:
        textarea 字段列表，章节原文作为占位内容；只检查末级章节，
        必填／选填标记向子节继承，必填指示优先；说明章节及其子节跳过。
        代码块、引用和列表内的标题不作为字段。
    """
    tokens = MarkdownIt("commonmark").parse(body)
    headings = [(i, int(token.tag[1:]), _label(tokens[i + 1].content))
                for i, token in enumerate(tokens)
                if token.type == "heading_open" and token.level == 0]
    sections = _sections(body)
    instructions = {"填写说明", "提交说明", "使用说明", "instructions", "submissioninstructions"}
    optional = re.compile(r"(?:[（(\[]\s*(?:选填|可选|optional)\s*[）)\]]|(?:^|[：:])\s*(?:选填|可选|optional))\s*$", re.IGNORECASE)
    parents: list[tuple[int, bool, bool, bool]] = []
    fields = []
    for index, (start, level, label) in enumerate(headings):
        parents = [parent for parent in parents if parent[0] < level]
        label_key = "".join(label.split())
        is_optional = bool(optional.search(label_key)) or any(parent[1] for parent in parents)
        is_instruction = optional.sub("", label_key).casefold() in instructions or any(parent[2] for parent in parents)
        explicitly_required = _required_hint(label) or any(parent[3] for parent in parents)
        parents.append((level, is_optional, is_instruction, explicitly_required))
        # 有子标题的章节作为分组，避免重复检查或要求选填分组必须填写。
        if is_instruction or (index + 1 < len(headings) and headings[index + 1][1] > level):
            continue
        section = sections.get(label_key, "")
        end = headings[index + 1][0] if index + 1 < len(headings) else len(tokens)
        # HTML 注释中的填写要求也有效，但代码示例中的词语不作为必填指示。
        guidance = " ".join(token.content for token in tokens[start + 3:end]
                            if token.type in {"inline", "html_block"})
        fields.append({"type": "textarea", "attributes": {
            "label": label, "placeholder": section},
            "validations": {"required": explicitly_required or _required_hint(guidance)
                            or (required is not False and not is_optional)}})
    return fields


def _option_label(value: Any) -> str:
    # 官方示例包含未加引号的版本号；展示时将数值转回文字。
    """将下拉选项转换为显示文字。

    Args:
        value: 选项文字或数值版本号。
    Returns:
        原始文字或数值转成的字符串。
    Raises:
        ValueError: 选项为空、类型无效或数值不是有限值。
    """
    if isinstance(value, str) and value.strip():
        return value
    if type(value) is int or (type(value) is float and math.isfinite(value)):
        return str(value)
    raise ValueError('选项需要非空文字或版本号')


def _fields(template: dict[str, Any], *, expand_markdown: bool = True) -> list[dict[str, Any]]:
    """校验并提取输入字段，markdown 仅用于识别紧邻复选框的全部勾选要求。

    Args:
        template: 已解析的模板字典。
        expand_markdown: 是否展开文本框中的 Markdown 章节；复制模板时关闭以保留原文。
    Returns:
        规范化后的字段副本列表；下拉选项统一为字符串，含 Markdown 章节的文本框按章节展开，不依赖 label。关闭展开时用 markdown_body 保留原文。原模板不变。
    Raises:
        ValueError: body、字段类型或属性格式无效。
    """
    index = None
    try:
        body = template.get('body', [])
        if not isinstance(body, list):
            raise ValueError('body 必须为数组')
        result = []
        for index, original in enumerate(body):
            if not isinstance(original, dict):
                raise ValueError('字段必须为对象')
            field = deepcopy(original)
            kind = field.get('type')
            if kind not in {'markdown', 'input', 'textarea', 'dropdown', 'checkboxes'}:
                raise ValueError('不支持的表单元素类型')
            attrs = field.get('attributes')
            if not isinstance(attrs, dict):
                raise ValueError('attributes 必须为对象')
            if kind == 'markdown':
                if not isinstance(attrs.get('value'), str):
                    raise ValueError('markdown.value 必须为文字')
                continue
            for key in ('description', 'placeholder', 'value', 'render'):
                if attrs.get(key) is not None and not isinstance(attrs[key], str):
                    raise ValueError('字段文字属性无效')
            validations = field.setdefault('validations', {})
            if not isinstance(validations, dict) or not isinstance(validations.get('required', False), bool):
                raise ValueError('validations.required 必须为布尔值')
            label = attrs.get('label')
            if label is not None and not isinstance(label, str):
                raise ValueError('字段 label 必须为文字')
            # 配置和文字取并集：显式 false 不能抵消字段本身的必填说明。
            if _required_hint(label, attrs.get('description'), attrs.get('placeholder')):
                validations['required'] = True
            # 外层必填表示文本框需要内容，具体章节仍各自识别必填／选填。
            sections = (_markdown_fields(attrs.get('value') or '',
                        required=validations.get('required'))
                        if kind == 'textarea' and not attrs.get('render') else [])
            if sections:
                if expand_markdown:
                    result.extend(sections)
                else:
                    field['markdown_body'] = attrs['value']
                    result.append(field)
                continue
            if not label or not label.strip():
                raise ValueError('字段缺少可检查的 label 或 Markdown 填写章节')
            if kind in {'input', 'textarea'} and not attrs.get('render') and _required_hint(attrs.get('value')):
                validations['required'] = True
            if kind in {'dropdown', 'checkboxes'}:
                options = attrs.get('options')
                if not isinstance(options, list) or not options:
                    raise ValueError('options 必须为非空数组')
                if kind == 'dropdown':
                    # 文档既给出字符串列表，也给出 label 对象形式。
                    attrs['options'] = [_option_label(o.get('label') if isinstance(o, dict) else o)
                                        for o in options]
                    if not isinstance(attrs.get('multiple', False), bool):
                        raise ValueError('multiple 必须为布尔值')
                    if 'default' in attrs and (type(attrs['default']) is not int
                            or not 0 <= attrs['default'] < len(options)):
                        raise ValueError('default 必须为有效选项索引')
                else:
                    # 仅将紧邻的说明应用于当前组，不影响后续其他复选框。
                    previous = body[index - 1] if index else {}
                    notice = previous['attributes']['value'] if previous.get('type') == 'markdown' else ''
                    all_required = any(re.search(
                        r'(?:必须|务必|请)(?:全部|所有)(?:确认(?:且|并))?勾选',
                        ''.join(_label(token.content).split()))
                        for token in MarkdownIt("commonmark").parse(notice) if token.type == 'inline')
                    for option in options:
                        if (not isinstance(option, dict) or not isinstance(option.get('label'), str)
                                or not option['label'].strip()
                                or not isinstance(option.get('required', False), bool)):
                            raise ValueError('复选项需要 label 和可选的布尔 required')
                        option['required'] = all_required or option.get('required', False) or _required_hint(option['label'])
            result.append(field)
        return result
    except ValueError as exc:
        location = f'body 第 {index + 1} 项：' if index is not None else ''
        raise ValueError(location + str(exc)) from None
    except (TypeError, KeyError, AttributeError):
        raise ValueError('模板字段或属性格式无效') from None


def parse_template(content: str, filename: str) -> dict[str, Any]:
    """解析 YAML 或 Markdown 模板并校验字段。

    Args:
        content: 模板文件的 UTF-8 文本。
        filename: 模板文件名，通过扩展名区分 YAML 和 Markdown。
    Returns:
        模板字典；Markdown 章节转为默认必填的文本字段，另保留 format 和
        markdown_body。明确选填、说明章节和分组标题不产生必填要求。
    Raises:
        ValueError: 模板信息、字段属性或 Markdown 头部配置无效。
        yaml.YAMLError: YAML 语法错误或包含不允许的标签。
    """
    content = content.lstrip('\ufeff')
    if PurePosixPath(filename).suffix.lower() == '.md':
        metadata = {}
        if content.startswith('---\n') or content.startswith('---\r\n'):
            lines = content.splitlines(keepends=True)
            end = next((i for i in range(1, len(lines)) if lines[i].strip() in {'---', '...'}), None)
            if end is None:
                raise ValueError('Markdown front matter 未闭合')
            metadata = yaml.load(''.join(lines[1:end]), Loader=TemplateLoader)
            if metadata is None:
                metadata = {}
            if not isinstance(metadata, dict):
                raise ValueError('Markdown front matter 必须为对象')
            content = ''.join(lines[end + 1:])
        template = {**metadata, 'name': metadata.get('name') or PurePosixPath(filename).stem,
                    'description': metadata.get('about', metadata.get('description', '')),
                    'body': _markdown_fields(content), 'format': 'markdown', 'markdown_body': content}
    else:
        template = yaml.load(content, Loader=TemplateLoader)
    if (not isinstance(template, dict) or not isinstance(template.get('name'), str)
            or not template['name'].strip()):
        raise ValueError('模板 name 必须为非空文字')
    if not isinstance(template.get('title', ''), str):
        raise ValueError('模板 title 必须为文字')
    if not isinstance(template.get('description', ''), str):
        raise ValueError('模板 description 必须为文字')
    _fields(template)
    return template

