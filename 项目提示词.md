# Beacon Shell

这是一个利用闲置算力的工具，让Agent可以把计算任务交给闲置算力做。

闲置算力包括：手机Termux、免费的云IDE、云NoteBook。

这些闲置算力没有独立的公网IP，不方便通过ssh远程操作，于是就有了besh。

## besh的四个角色

### 信标（云服务器，有公网IP）

信标用于中转命令、记录系统状态、并且提供Web操作界面。

```bash
export besh_secret=CHANGE_TO_YOUR_PASSWORD
besh serve
```
这会通过创建./besh_beacon.py作为主程序，运行既为服务端。

### 工人（任意终端，无公网IP）

工人向信标轮询命令，发送到对应的tmux窗口执行。

```bash
export besh_url=http://BEACON-HOST:12242?secret=CHANGE_TO_YOUR_PASSWORD
besh worker WORKER_ID
```



### 遥控器（本地电脑）

遥控器就类似于tmux，唯一区别在于把session（操作多个会话）替换为工人ID（操作多个工人）

```bash
export besh_url=http://BEACON-HOST:12242?secret=CHANGE_TO_YOUR_PASSWORD
# 列出所有工人
besh ls
besh list-sessions
besh list-workers
# 列出所有窗口
besh list-windows -t WORKER_ID
# 查看指定窗口
besh capture-pane -t WORKER_ID:WINDOW_NAME -p -S -50
# 发送按键到指定窗口（窗口不存在则自动创建）
besh send-keys -t WORKER_ID:WINDOW_NAME "ls -l" Enter
# 发送按键到指定窗口，加 -l 把命令当字面意思处理
besh send-keys -t WORKER_ID:WINDOW_NAME -l 'echo "Hello $USER"'
```

### 对象存储（Cloudflare R2）

假设工人是临时的、会随时释放，所以要备份工人的工作目录。

信标（公网服务器）带宽贵，网速慢，不适合备份工作目录。

[Cloudflare R2](https://developers.cloudflare.com/r2/) 提供免费10GB的存储空间，网速快，所以用它备份工作目录。

1. 开通Cloudflare R2对象存储，创建存储桶：`besh`
2. 安装[rclone](https://rclone.org/install/)
```bash
sudo -v ; curl https://rclone.org/install.sh | sudo bash
```
3. 配置rclone的环境变量，用来访问Cloudflare R2对象存储
```bash
export RCLONE_CONFIG_CF_TYPE=s3
export RCLONE_CONFIG_CF_PROVIDER=Cloudflare
export RCLONE_CONFIG_CF_ACCESS_KEY_ID=
export RCLONE_CONFIG_CF_SECRET_ACCESS_KEY=
export RCLONE_CONFIG_CF_ENDPOINT=
export RCLONE_CONFIG_CF_REGION=auto
```

工人的工作目录存储于`cf:besh/YYYY-MM-DD/WORKER_ID/*`


## 安装

```bash
pip install besh
```

## 实现细节
1. 信标采用 [sqless](https://github.com/cqian-cs/sqless) 作为后端框架（简易单一用户认证+数据库读写+函数转API+函数转MCP）
    - 运行`besh serve`，会在当前路径下通过`sqless --app besh_beacon.py`创建服务端代码`besh_beacon.py`
2. 工人本身就处在容器/虚拟机内，因此不提供安全隔离机制。
    - 运行`besh worker WORKER_ID` 会通过`mkdir ./WORKER_ID; cd ./WORKER_ID` 进入工作目录，后续轮询到的指令都在此目录下执行。
    - 工人会通过tmux启动一个`besh_sync`的session用于同步工作目录到对象存储
    - 工人会通过tmux启动一个`besh_worker`的session用于创建窗口、执行命令
3. http://BEACON-HOST:12242?secret=CHANGE_TO_YOUR_PASSWORD 会提供每个worker的状态，以及完善的WebShell、文件上传下载（基于对象存储）。
4. 遥控器为AI Agent提供命令行。AI Agent既可以通过信标MCP操作，又可以通过命令行SKILL操作。