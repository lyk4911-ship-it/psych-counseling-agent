当前基线版本

- 保存时间：2026-04-28 13:20 左右
- 目标定位：更像倾听者，而不是引导者
- 固定评测结果：
  - `overall_avg_score = 7.0`
  - `overall_human_likeness_score = 7.52`
- 对应评测目录：`simulation_logs/fixed_eval_20260428_131355`

包含文件：

- `dialog_agent.py`
- `agent_rules.py`
- `run_fixed_eval.py`
- `summary.json`

回滚方式：

1. 用此目录中的同名文件覆盖项目根目录对应文件。
2. 重新运行：

```powershell
& "c:/Users/lyk49/Desktop/心理咨询/.venv/Scripts/python.exe" "c:/Users/lyk49/Desktop/心理咨询/run_fixed_eval.py"
```

3. 若总分和真人感分恢复接近：
   - `overall_avg_score = 7.0`
   - `overall_human_likeness_score = 7.52`

说明当前代码已回到该基线附近。
