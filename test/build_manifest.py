"""Rebuild fixture labels from unique source anchors; never execute fixtures."""

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LABELS = []


def issue(file, anchor, title, condition, category, rules=(), occurrence=None):
    lines = (ROOT / "cases" / file).read_text().splitlines()
    matches = [i for i, line in enumerate(lines, 1) if anchor in line]
    if len(matches) < (occurrence or 1):
        raise ValueError(f"Missing source anchor: {file}: {anchor}")
    if occurrence is None and len(matches) != 1:
        raise ValueError(f"Ambiguous source anchor: {file}: {anchor}")
    LABELS.append({"id": f"{file.split('/')[0].upper()}-{sum(r['file'].split('/')[0] == file.split('/')[0] for r in LABELS) + 1:02}",
                   "file": file, "line": matches[(occurrence or 1) - 1], "title": title,
                   "condition": condition, "category": category, "static_rules": list(rules)})


def build():
    LABELS.clear()
    issue("c/defects.c", "return values[4]", "数组读取越界", "合法下标是 0–3，执行该函数即越界。", "memory", ["arrayIndexOutOfBounds"])
    issue("c/defects.c", "values[2] = 7", "数组写入越界", "数组只有两个元素，合法下标是 0–1。", "memory", ["arrayIndexOutOfBounds"])
    issue("c/defects.c", "42 / divisor", "整数除零", "divisor 已被赋值为 0。", "arithmetic", ["zerodiv"])
    issue("c/defects.c", "return *value;", "空指针解引用", "value 为 NULL。", "memory", ["nullPointer"], occurrence=1)
    issue("c/defects.c", "return *value;", "释放后读取", "分配成功后释放，再解引用已释放地址。", "memory", ["deallocuse", "deallocret"], occurrence=2)
    issue("c/defects.c", "free(buffer);", "重复释放", "分配非空地址后第二次 free；free(NULL) 本身无害。", "memory", ["doubleFree"], occurrence=2)
    issue("c/defects.c", "return total + 1", "读取未初始化变量", "total 从未赋值。", "initialization", ["uninitvar"])
    issue("c/defects.c", "return &result", "返回栈变量地址", "函数返回后 result 的生命周期结束。", "memory", ["returnDanglingLifetime", "returnAddressOfAutoVariable"])
    issue("c/defects.c", "return *value;", "堆内存泄漏", "分配成功的 value 在返回前没有释放，也没有转移所有权。", "memory", ["memleak"], occurrence=3)
    issue("c/defects.c", 'strcpy(buffer, "longer', "字符串复制溢出", "包括终止符在内的输入长度大于目标容量 4。", "memory", ["bufferAccessOutOfBounds"])
    issue("c/logic.c", "return sum / count", "平均值发生整数截断", "例如 values=[1,2]、count=2，预期 1.5，返回 1.0。", "logic")
    issue("c/logic.c", "return sum / count", "空输入导致整数除零", "count=0 时除数为零；约定应拒绝空输入。", "arithmetic")
    issue("c/logic.c", "subtotal > 100", "免邮边界错误", "约定满 100 免邮，subtotal=100 时应返回 0，实际返回 10。", "contract")
    issue("c/logic.c", "int result = 0", "全负数集合最大值错误", "约定输入非空；[-5,-2] 的最大值应为 -2，实际返回 0。", "logic")
    issue("c/logic.c", "low = middle", "二分查找不推进，可能无限循环", "有序数组 [1,3] 查找 2 时 low=0、high=1，middle=0，low 不变。", "termination")
    issue("c/syntax_error.c", "int broken_sum", "函数参数缺少右括号", "解析阶段；可能遮蔽后续错误。", "syntax", ["syntaxError"])
    issue("c/syntax_error.c", "int total =", "赋值语句缺少分号", "先补全参数列表，随后该行仍缺少分号。", "syntax")

    issue("cpp/defects.cpp", "return values[3]", "数组读取越界", "合法下标是 0–2。", "memory", ["arrayIndexOutOfBounds"])
    issue("cpp/defects.cpp", "return *value;", "空指针解引用", "value 为 nullptr。", "memory", ["nullPointer"], occurrence=1)
    issue("cpp/defects.cpp", "return result + 2", "读取未初始化变量", "result 从未赋值。", "initialization", ["uninitvar"])
    issue("cpp/defects.cpp", "delete values", "new[] 与 delete 不匹配", "数组分配需要匹配 delete[]。", "memory", ["mismatchAllocDealloc"])
    issue("cpp/defects.cpp", "return *value;", "delete 后读取", "value 已在上一行释放。", "memory", ["deallocuse", "deallocret"], occurrence=2)
    issue("cpp/defects.cpp", "delete value;", "重复 delete", "同一非空分配被释放两次。", "memory", ["doubleFree"], occurrence=3)
    issue("cpp/defects.cpp", "return value;", "返回局部变量引用", "函数返回后局部变量生命周期结束，引用悬空。", "memory", ["returnDanglingLifetime", "returnReference"])
    issue("cpp/defects.cpp", "return *value;", "new 分配未释放", "函数只返回数值，分配的内存失去所有者。", "memory", ["memleak"], occurrence=3)
    issue("cpp/defects.cpp", "return *first", "使用已经失效的迭代器", "reserve 的参数大于当前 capacity，发生重新分配后原迭代器失效。", "memory", ["invalidContainer", "invalidContainerReference", "invalidIterator1", "invalidIterator2", "invalidIterator3"])
    issue("cpp/defects.cpp", "values[2] = 9", "数组写入越界", "数组只有两个元素。", "memory", ["arrayIndexOutOfBounds"])
    issue("cpp/logic.cpp", "return total / values.size", "平均值发生整数截断", "[1,2] 的预期平均值为 1.5，整数除法得到 1。", "logic")
    issue("cpp/logic.cpp", "return total / values.size", "空向量导致整数除零", "values.empty() 时 size() 为零。", "arithmetic")
    issue("cpp/logic.cpp", "std::unique", "去重后没有删除尾部", "约定返回去重集合；[1,1,2] 去重后 size 应为 2，实际仍为 3。", "logic", ["ignoredReturnValue", "uselessCallsRemove"])
    issue("cpp/logic.cpp", "int result = 0", "全负数集合最大值错误", "非空输入 [-5,-2] 返回 0，而不是 -2。", "logic")
    issue("cpp/logic.cpp", "subtotal > 100", "免邮边界错误", "约定满 100 免邮；100 被收取运费。", "contract")
    issue("cpp/syntax_error.cpp", "return 3", "return 语句缺少分号", "解析阶段。", "syntax", ["syntaxError", "internalAstError"])
    issue("cpp/syntax_error.cpp", "}", "类定义末尾缺少分号", "修复成员函数语法后，类结尾仍应为 };。", "syntax", occurrence=2)

    issue("python/defects.py", "return eval", "不可信表达式执行", "text 来自不可信输入时可执行任意 Python 表达式。", "security", ["B307"])
    issue("python/defects.py", "pickle.loads", "不可信反序列化", "payload 不可信时可能执行代码。", "security", ["B301"])
    issue("python/defects.py", "subprocess.run", "Shell 命令注入", "command 拼入不可信内容时，shell=True 允许解释 Shell 语法。", "security", ["B602"])
    issue("python/defects.py", "assert user.is_admin", "使用 assert 做权限检查", "Python 以优化模式运行时断言可被移除，非管理员也可能得到数据。", "security", ["B101"])
    issue("python/defects.py", "random.randint", "密码重置令牌使用非密码学随机数", "该返回值用作安全令牌时，普通伪随机数不适合安全用途。", "security", ["B311"])
    issue("python/defects.py", "missing_fee", "引用未定义名称", "调用 order_total 时缺少全局名称 missing_fee。", "name", ["F821", "reportUndefinedVariable"])
    issue("python/defects.py", "values.push", "list 不存在 push 方法", "values 为 Python list 时触发 AttributeError。", "type")
    issue("python/defects.py", 'return "average="', "字符串与数值直接相加", "非空数值列表计算出 average 后，拼接字符串触发 TypeError。", "type", ["reportOperatorIssue"])
    issue("python/defects.py", "history=[]", "可变默认参数跨调用共享", "先 remember(1)，再 remember(2)，第二次得到 [1,2]，而约定每次默认新建历史。", "state")
    issue("python/defects.py", "total += value", "混合列表累加字符串", "运行至字符串 '30' 时 int + str 触发 TypeError。", "type")
    issue("python/logic.py", "return sum(values)", "空列表求均值除零", "mean([]) 触发 ZeroDivisionError，约定应显式拒绝空输入。", "arithmetic")
    issue("python/logic.py", "values[index + 1]", "相邻元素访问越界", "任何非空列表的最后一次循环访问越界；空列表不会进入循环。", "bounds")
    issue("python/logic.py", "subtotal > 100", "免邮边界错误", "约定满 100 免邮；shipping_fee(100) 错误返回 10。", "contract")
    issue("python/logic.py", "result = 0", "全负数集合最大值错误", "非空列表 [-5,-2] 返回 0，而不是 -2。", "logic")
    issue("python/logic.py", "return [[0]", "矩阵各行共享同一列表", "fresh_matrix(2,2) 后修改一行的元素，会同时修改另一行。", "state")
    issue("python/syntax_error.py", "def process_data", "函数定义缺少冒号", "解析阶段；后面的拼接与未定义变量供恢复审查观察，但未作为独立计分项。", "syntax", ["invalid-syntax"])

    issue("javascript/defects.js", "return eval", "不可信表达式执行", "text 不可信时执行任意 JavaScript。", "security", ["no-eval"])
    issue("javascript/defects.js", "new Function", "不可信内容构造动态函数", "expression 不可信且生成的函数被调用时可能执行注入代码。", "security", ["no-new-func"])
    issue("javascript/defects.js", "missingFee", "引用未定义名称", "调用 calculateTotal 时缺少 missingFee。", "name", ["no-undef"])
    issue("javascript/defects.js", "total = 2", "重新赋值 const", "调用 changeConstant 时对常量赋值。", "type", ["no-const-assign"])
    issue("javascript/defects.js", "value === value", "可疑的自身比较", "比较没有使用第二个值；NaN 与自身比较为 false，其余通常为 true，应核对比较意图。", "suspicious", ["no-self-compare"])
    issue("javascript/defects.js", 'console.log("charged")', "return 后的不可达语句", "前一行无条件返回，此语句永不执行。", "control_flow", ["no-unreachable"])
    issue("javascript/defects.js", "retries: 1, retries", "重复对象键覆盖配置", "后面的 retries 覆盖前面的值。", "state", ["no-dupe-keys"])
    issue("javascript/defects.js", "return true", "finally 返回值吞掉异常", "try 抛出错误后，finally 的 return 覆盖异常。", "control_flow", ["no-unsafe-finally"])
    issue("javascript/defects.js", "new Promise(async", "异步 Promise executor 可能丢失异常", "loader 拒绝时异步 executor 返回的 Promise 被外层构造器忽略，外层可能一直 pending。", "async", ["no-async-promise-executor"])
    issue("javascript/defects.js", "return user.name", "读取 null 的属性", "user 明确为 null。", "type")
    issue("javascript/logic.js", "return [...values].sort()", "数值排序使用默认字典序", "约定数值升序；[2,10,1] 返回 [1,10,2]。", "logic")
    issue("javascript/logic.js", "values[index + 1]", "最后一对含越界 undefined", "非空数组最后一次迭代读取越界位置；JavaScript 此处返回 undefined，不是必然抛出异常。", "bounds")
    issue("javascript/logic.js", "subtotal > 100", "免邮边界错误", "约定满 100 免邮；100 被收取运费。", "contract")
    issue("javascript/logic.js", "values.map", "map 回调缺少 return", "[1,2] 预期得到 [2,4]，实际得到 [undefined,undefined]。", "logic")
    issue("javascript/logic.js", "values.reduce", "空数组均值为 NaN", "mean([]) 得到 0/0 即 NaN，约定应拒绝空输入；不是 Python 式除零异常。", "arithmetic")
    issue("javascript/syntax_error.js", "function processData", "函数参数缺少右括号", "解析阶段；后续未定义变量可能被遮蔽。", "syntax", ["parse-error"])

    issue("typescript/defects.ts", "return eval", "不可信表达式执行", "text 来自不可信输入时可执行代码。", "security", ["no-eval"])
    issue("typescript/defects.ts", "new Function", "不可信内容构造动态函数", "expression 不可信且生成函数被调用时可能注入代码。", "security", ["no-new-func"])
    issue("typescript/defects.ts", 'const count: number = "three"', "string 赋给 number", "TypeScript 类型检查应拒绝；当前工具没有 tsc，因此可能漏报。", "type")
    issue("typescript/defects.ts", 'return "42"', "返回类型不匹配", "声明返回 number，实际返回 string。", "type")
    issue("typescript/defects.ts", "return user.email", "访问类型中不存在的属性", "参数只声明 name，email 不存在。", "type")
    issue("typescript/defects.ts", "return user.name", "可空对象未检查", "strictNullChecks 开启时类型检查应拒绝；传入 null 后会抛异常。", "type")
    issue("typescript/defects.ts", "return value()", "将 number 当作函数调用", "value 明确为数值 42，不可调用。", "type")
    issue("typescript/defects.ts", "value.toUpperCase", "调用 number 不存在的方法", "toUpperCase 是字符串方法。", "type")
    issue("typescript/defects.ts", "count = 2", "重新赋值 const", "常量不能被重新赋值。", "type", ["no-const-assign"])
    issue("typescript/defects.ts", "return true", "finally 返回值吞掉异常", "try 抛出的错误被 finally 的 return 覆盖。", "control_flow", ["no-unsafe-finally"])
    issue("typescript/logic.ts", "return [...values].sort()", "数值排序使用默认字典序", "[2,10,1] 预期 [1,2,10]，实际 [1,10,2]。", "logic")
    issue("typescript/logic.ts", "return values[0]", "空数组返回 undefined", "firstValue([]) 返回 undefined，与 number 返回约定冲突；noUncheckedIndexedAccess 开启时应暴露类型问题。", "bounds")
    issue("typescript/logic.ts", "subtotal > 100", "免邮边界错误", "约定满 100 免邮；100 被收取运费。", "contract")
    issue("typescript/logic.ts", "values.map", "map 回调缺少 return", "[1,2] 得到 [undefined,undefined]，而约定翻倍为 [2,4]。", "logic")
    issue("typescript/logic.ts", "values.reduce", "空数组均值为 NaN", "mean([]) 返回 NaN，约定应拒绝空输入。", "arithmetic")
    issue("typescript/syntax_error.ts", "function processData", "参数列表结束处缺少右括号", "解析阶段；后面的未定义名称可能被遮蔽。", "syntax", ["parse-error"])

    cases = []
    for path in sorted((ROOT / "cases").glob("*/*")):
        if path.is_file():
            name = path.relative_to(ROOT / "cases").as_posix()
            cases.append({"file": name, "language": name.split("/")[0],
                          "group": path.stem, "issues": [row for row in LABELS if row["file"] == name]})
    manifest = {"schema_version": 1, "description": "人工设计的教学缺陷标签，包含条件性风险和显式业务约定；不是标准基准或动态复现结果。",
                "cases": cases}
    (ROOT / "expected.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"生成 {len(cases)} 个用例，{len(LABELS)} 个预期问题标签。")


if __name__ == "__main__":
    build()
