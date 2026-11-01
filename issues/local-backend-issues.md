# 本地/自托管后端接入 —— 完整问题清单与实测证据

> 来源：2026-10-01 在本机（GTX 1060 Max-Q 6 GiB / 15.6 GiB RAM）把 py harness 接到
> **本地 llama.cpp 开源小模型**（Sharp-Spark-X2.5-4B Q6_K_XL，`llama-server`，128K ctx）
> 时实际踩到并复现的问题。I-47 只实现其中的 L1-01/02/03；其余为待决策项。

---

Harness 接入核查 (OH_workspace, 2026-10-01)
==========================================================
结论: 128K 装得下 agentic 工作负载, 但接入前必须修 3 处。

[1] 原生 tool calling: 可用
    tools 参数实测 -> finish_reason=tool_calls
    tool_calls=[{"function":{"name":"list_dir","arguments":"{\"path\":\"/tmp\"}"}}]
    chat_template.jinja 自带 ## Tools 块 + <tools> 标签, 且有 FIX 2 顺序修正

[2] 服务端默认 base_url 是 DeepSeek 云端
    harness/config/local.yaml: base_url: "https://api.deepseek.com/v1"
    harness/sdk/config.py:31,56 同样默认
    -> 需改指 http://127.0.0.1:8080/v1 (api_key 任意非空)

[3] 严重: harness 的 token 预算用错 tokenizer
    harness/prompt_assembly.py:20-30
      _get_tokenizer(provider="deepseek") -> AutoTokenizer.from_pretrained("tokenizer_dir/deepseek")
    tokenizer_dir/deepseek/ : BPE vocab 128000, 含 CJK 词表项 = 0
    模型实际: tokenizer.ggml.model=gpt2, pre=spark2_5, vocab 131072

    实测偏差 (deepseek 计数 vs 模型真实 prompt_tokens):
      英文散文          25  vs  16   +56% 高估
      Python 代码       28  vs  35   -20% 低估
      JSON 工具schema   44  vs  56   -21% 低估
      纯中文           0  vs  49   -100% 低估  <-- 整段算 0
      中英混合          7  vs  15   -53% 低估
      中英混合(harness风格) 16 vs 26 -38% 低估

    后果: budget_tokens = 128000*0.8 = 102400 按错误计数执行
          - 中文/混合内容被严重低估 -> 实际 prompt 可能冲破 102400
            甚至冲破 n_ctx=131072 -> 服务端 400 (或被预填到 90% 才取消)
          - 代码/JSON 被高估 -> 历史被过早截断, 浪费上下文

    修复: 让 _count_tokens 走模型自己的 tokenizer, 或直接调 /tokenize
          (llama.cpp /tokenize 端点实测可用, POST {"content": "..."} -> {"tokens":[...]})
          provider 需新增 "spark2_5" 对应的 tokenizer_dir 子目录,
          或改为查 HF: XHToken/Spark-X2.5-4B 的 tokenizer。

[4] 性能: 深上下文生成 ~6.5 t/s
    KV 在 RAM, 每个生成 token 都要经 PCIe 搬 K/V。
    KV ~180 token 时 21.3 t/s; KV ~39611 token 时 6.5 t/s。
    agentic 每一步 = 一次 prefill + 一次生成, 深上下文下生成明显变慢。

---

## 附：本地部署记录（环境坑）

Harness 本地接入 —— 已完成配置与验证 (2026-10-01)
==========================================================

服务: llama-server 128K ctx / q4_0 KV / VRAM 4356 MiB
       scripts/serve.sh status  ->  running pid=34533 ctx=131072

一、拷贝与接线
  harness/  tests/  registry/  skills/  docs/  pyproject.toml  Makefile
  venv 的 editable finder 原指向 /home/gexingdeyun/OH_workspace/harness,
  已改为 /mnt/disk2/ds_workspace_infer_engine/harness
  (.venv/lib/python3.13/site-packages/__editable___agent_harness_0_1_0_finder.py)

二、新增配置
  configs/llm.yaml            profile "default"/"local_fast" -> 本地 8080
  harness/config/models.yaml  model service "spark-x2.5-4b"  <- 缺它会报
                              "model service not registered" (local backend 走 pool)
  harness/config/sandbox.yaml 路径改指本工作区

三、本次踩到并已固化的 5 个环境坑 (scripts/run_harness.sh 全部处理)
  1. cwd 必须是工作区 —— HarnessClient 用 Path.cwd() 当 project_dir,
     去找 configs/llm.yaml 与 harness/config/*.yaml,./sessions/ 也落在这
  2. HOME 必须"赋值"而非 setdefault —— 实例注册表走 Path.home()
     (~/.harness/instances);本环境 HOME 已存在,setdefault 不生效,
     会写到工作区外只读路径 -> OSError Errno 30
  3. HF_HUB_OFFLINE=1 / TRANSFORMERS_OFFLINE=1 —— tokenizer_dir 是本地目录,
     缺文件时 transformers 会对 huggingface.co 重试 HEAD 5 次(网络不通),
     实测卡了 9 分钟
  4. PYTHONPATH=工作区,保证 import harness 命中本副本
  5. DEEPSEEK_API_KEY 需非空(llama-server 不校验,但 OpenAI SDK 要求)

四、代码改动 (2 处功能 + 1 处测试)
  1. harness/prompt_assembly.py — token 计数改为优先问本地服务
     原因:原实现固定用 tokenizer_dir/deepseek,而该词表【不含任何 CJK 字符】,
     纯中文整段算 0 token,预算形同虚设
     做法:POST <origin>/tokenize (注意在根路径,不是 /v1 下),失败则回退 HF;
           带结果 memo 与 30s 负缓存,避免服务不可达时每次重试
     效果(与真实 prompt_tokens 比):
           样本           旧误差    新误差
           英文散文        +56%      +6.2%
           纯中文         -100%      +5.9%
           中文长文       -100%      +0.8%
           中英混合        -38%      +3.8%
           Python          -20%      +2.9%
           JSON schema     -21%      +1.8%
           tool_result     -13%       0.0%
           最差 |误差|     100%  ->  6.2%   (残差只是 chat 模板开销)
     并且更快:一个真实 turn 的 43 次计数 1703ms -> 789ms

  2. harness/sdk/config.py + agent_runtime/impl.py — 两个 profile 通道
     * chat_template_kwargs: 透传 extra_body。
       Spark X2.5 模板默认 enable_thinking=true,思维链会吃掉 max_tokens
       并返回【空 content】;现在可在 profile 里统一关掉
     * temperature_override: 上游把 temperature 硬编码为 0.0(贪心),
       配置里的 temperature 根本不生效;现在可覆盖(默认 None 保持原行为)

  3. tests/test_sdk_prompts.py::test_build_messages_respects_budget_ratio
     原断言依赖脆弱体积假设:10 事件 x ~1006 token = 10060,
     而 128000*0.1 = 12800 已能全装下,于是 ratio 0.1 与 0.8 结果相同。
     改为按实测总 token 自适应缩放(0.4x / 3.0x),保住原意且不依赖体积

五、验证结果
  scripts/harness_smoke.py all  ->  {"config": true, "raw": true, "harness": true}
      [1] profile 解析到 http://127.0.0.1:8080/v1, ctk={enable_thinking: False}
      [2] OpenAI SDK 直连: content='PONG', in=178 out=3
      [3] 完整 harness: bubblewrap 沙箱内跑 bash, status=completed,
          output='HELLO_FROM_HARNESS'
  pytest tests/ -q  ->  695 passed, 7 failed, 2 skipped
      7 个失败全部是 401 认证错误,原因是这些测试按设计调用
      DeepSeek 云端 (tests/test_tools.py:196 "integration tests using real
      DeepSeek API",用 real_api_key fixture),本机无有效云 key,与本次改动无关。

六、沙箱限制模式下的注意事项
  workspace-write 模式下 /dev/nvidia* 被隐藏,nvidia-smi 会失败:
      "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver"
  这不影响【已在运行】的 llama-server —— 它在启动时(当时设备可见)就建好了
  CUDA context 并一直复用,不需要重新打开设备节点,推理正常(实测 20.7 t/s)。
  但【重启服务会失败】:新进程打不开 /dev/nvidia*。所以限制模式期间请勿重启,
  当前进程不可再生。
  因此 serve.sh 已改为:
    * 启动/停止不再依赖 nvidia-smi(原停止逻辑会因它失败而空等)
    * status 对 nvidia-smi 失败做降级显示
    * pidfile 自愈:旧版记录的是 start_server.sh 包装进程,现自动纠正为真实
      llama-server 进程

七、常用命令
  scripts/serve.sh status                 # 服务状态
  scripts/run_harness.sh scripts/harness_smoke.py all
  scripts/run_harness.sh -m pytest tests/ -q
  scripts/verify_tokenizer_fix.py         # token 计数精度对照
  scripts/measure_token_calls.py          # 计数开销测量
