# git_hack
git 文件泄露源码恢复工具。仅用于授权目标。

## Use
```
git clone https://github.com/tongchengbin/git_hack.git
pip install -r requirements.txt
python3 run.py -u https://example.com/.git
# 开启 debug 后显示每个网络请求的 URL、状态码和响应大小
python3 run.py -u https://example.com/.git --debug
# 项目默认下载到执行目录的 create 文件夹下
cd create
# 恢复到最后一次源码，可以使用 git log 查看提交历史
git reset --hard
```

目录索引返回 403 时，脚本会继续尝试直接下载常见 Git 元数据、refs、loose objects 和 pack 文件。
