# 自建 TVBox / OK影视 自动更新接口

把这套东西放进你自己的 GitHub 仓库，每天定时跑一次：抓上游 → 合并去重 → 挨个实测接口 → 输出一份 `tvbox.json`。
地址永远是你自己的，别人仓库跑路、改名、域名到期都不影响你。

整个过程不需要服务器，用的是 GitHub 免费提供的 Actions，建好之后基本不用再管。

---

## 一、它能做到什么

- 每天北京时间 05:00 自动重建，也可以随时在网页上手动点一次
- 上游源写在 `sources.txt` 里，一行一个；某个源哪天挂了，构建自动跳过它，不影响其它源
- 合并后按接口地址去重，避免同一个站重复出现
- 每个站点实测一遍 `api?ac=list`，实测通的排前面，探测环境够不到的（可能是国内站）保留但排后面，彻底没响应的剔除
- 自动下载 spider jar 并重算 md5 写进配置，OK影视 校验 md5 不会报错
- 站点里的相对路径（`./py/x.py`、`./js/x.js`）自动补成绝对地址，合并后不会串台
- 产出 `meta.json` 记录本次构建：哪些源成功、各并入多少站点、探活明细

实测一次跑完约 3–5 分钟，402 个站点上下（随上游浮动）。

---

## 二、放到自己仓库（三步）

### 第 1 步：新建仓库

GitHub 右上角 **+ → New repository**：

- Repository name：`tvbox-own-auto`（随便起，记住就行）
- 可见性必须选 **Public**（私有仓库用不了 jsDelivr 加速，raw 也能用但没意义）
- 勾 **Add a README file** 先不用管，建好后再覆盖
- 点 **Create repository**

### 第 2 步：上传文件

把本目录里的这些东西原样传上去，**目录结构不能变**：

```
tvbox-own-auto/
├── .github/workflows/update.yml     ← 定时任务，路径必须一字不差
├── scripts/build.py                 ← 构建脚本
├── sources.txt                      ← 上游源清单，以后改这里
├── tvbox.json                       ← 已经有了，先放一份能用的
└── meta.json                        ← 构建报告，可选
```

网页上传要点：`.github` 这种点开头的文件夹，用 **Add file → Create new file**，文件名直接敲 `.github/workflows/update.yml`，粘贴内容保存即可（网页会自动建目录）。

### 第 3 步：打开 Actions

1. 进仓库的 **Settings → Actions → General**
2. 底部 **Workflow permissions** 选 **Read and write permissions**，保存
   （不选这个，机器人提交会被拒）
3. 进 **Actions** 标签页，左边选 `update-tvbox-config`，右边 **Run workflow** 手动跑一次
4. 等 3–5 分钟，看到绿勾就成功了，仓库里的 `tvbox.json` 会刷新

> 首次建仓后 Actions 可能默认是关闭的，如果看不到 Run workflow 按钮，先在 Actions 页点一下 "I understand my workflows, go ahead and enable them"。

---

## 三、在 OK影视 里填地址

拿到你自己的地址，二选一：

**直连（总是最新，推荐）**
```
https://raw.githubusercontent.com/你的用户名/tvbox-own-auto/main/tvbox.json
```

**加速（国内更快，有最长 12 小时缓存）**
```
https://gcore.jsdelivr.net/gh/你的用户名/tvbox-own-auto@main/tvbox.json
```

OK影视 里的填法：**设置 → 配置地址 / 数据源 → 点播** 那一栏粘贴，确定后回首页，等它拉取。
配置里已经自带 `lives`（33 条直播线路），不用再单独往直播框填。

想立刻生效：把 OK影视 从多任务里划掉再打开，它会重新联网拉配置。

---

## 四、以后怎么维护（其实几乎不用）

- **加源 / 删源**：编辑 `sources.txt`，一行 `名称|配置地址`。第一行是主源，它的 spider jar 和直播、解析项作为骨架，后面的源只并入独立站点。改完提交，第二天自动生效，也可以手动跑一次。
- **只想换主源**：把想当骨架的那一行挪到最前面即可。
- **让盒子下载 jar 更快**：在 workflow 的构建步骤加一行环境变量
  ```yaml
  env:
    SPIDER_MIRROR: https://gh-proxy.com/
  ```
  这样配置里的 jar 地址会走加速镜像，md5 不变（实测镜像与源文件 md5 一致）。

---

## 五、常见问题

**Q：Actions 跑失败了怎么办？**
点进失败的运行看日志，多半是某个上游拉不动（会自动跳过），或者 GitHub 抽风。重跑一次通常就好。`tvbox.json` 会保持上一版，不会变空。

**Q：电视上打开一片空白 / 提示配置获取失败？**
先确认用浏览器能打开你的地址；能打开就把地址换成 jsDelivr 那一版试试。jar 加载慢也会导致首页空白，等 10 秒或换 `SPIDER_MIRROR`。

**Q：站点数怎么每天不一样？**
这是正常的，探活结果每天不同，死站会被剔掉，新站会补进来。

**Q：能不能放 Gitee？**
可以托管文件，但 Gitee 的自动任务是 Gitee Go，配置方式和 GitHub Actions 不通用，且 Gitee Pages 免费版已不对外。稳妥做法还是 GitHub 托管 + jsDelivr 加速。

**Q：不想自己写代码，只想 Fork 一个现成的？**
那就直接 Fork `longge83/tvbox-merged`：它自带完整的 45 分钟流水线，输出 10 份配置（精播版 / 国内极速版 / 7 天稳定版等），Fork 后开一下 Actions 和 Pages 就能用。代价是每次跑得久、代码量大，改起来不如这套轻。

---

## 六、说明

所有资源均来自公开上游配置，本项目只做格式合并与可达性探测，不存储、不转码任何影视内容；请自行判断使用场景并遵守当地法律法规。
