# 本地镜像源离线包

在联网电脑上准备 `fastgpt-mirror-manager:1.0.0-amd64` 镜像。可以从源码构建，也可以用 `docker load -i` 加载包含该镜像的归档。将 `mirror-bundle` 放到依赖清单所在目录，从该目录运行以下命令。

~~~bash
bash mirror-bundle build --profile cp312 --input requirements.in --output python-cp312.tar.gz
bash mirror-bundle build --profile cp310 --input requirements.in --output python-cp310.tar.gz
bash mirror-bundle check python-cp312.tar.gz
~~~

Python 每行填写一个 PEP 508 依赖。请使用精确版本，例如 requests==2.32.5。

~~~bash
bash mirror-bundle build --profile node24 --input package.json --output npm-node24.tar.gz
bash mirror-bundle check npm-node24.tar.gz
~~~

NPM 使用现有的 v3 package-lock.json，文件缺失时生成锁。收集 Linux AMD64/glibc、Node 24 的生产依赖、peer 依赖及适用的 optional 依赖。

管理员在内网页上传生成的 tar.gz，确认预检通过后发布。已有同内容的文件会跳过。仓库保留既有 NPM 默认版本，安装时建议明确指定新版本。

仅交付匹配环境的 wheel 和完整依赖。sdist、Git/本地路径依赖、安装脚本、系统组件和额外下载需要另行评审。通过预检代表依赖可以离线解析或安装，业务代码仍需在目标沙箱验证。npm ci 使用与应用清单匹配的锁，锁中的绝对下载地址须指向目标内网源。
