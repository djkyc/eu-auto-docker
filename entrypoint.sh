#!/bin/bash

# 如果设置了 RUN_NOW=true，立即运行一次
if [ "$RUN_NOW" = "true" ]; then
    echo "$(date): 立即执行续期脚本..."
    /usr/local/bin/python3 /app/euser_renew.py
fi

# 如果设置了定时任务模式
if [ "$CRON_SCHEDULE" != "" ]; then
    echo "$(date): 设置定时任务: $CRON_SCHEDULE"
    
    # 将环境变量写入文件供 cron 使用
    printenv | grep -E "EUSERV_|TG_|EMAIL_" > /app/.env
    
    # 创建 cron 任务
    echo "$CRON_SCHEDULE cd /app && export \$(cat /app/.env | xargs) && /usr/local/bin/python3 /app/euser_renew.py >> /var/log/euserv.log 2>&1" > /etc/cron.d/euserv
    chmod 0644 /etc/cron.d/euserv
    crontab /etc/cron.d/euserv
    
    # 创建日志文件
    touch /var/log/euserv.log
    
    echo "$(date): 定时任务已设置，容器将持续运行..."
    
    # 启动 cron 并保持容器运行，同时输出日志
    cron && tail -f /var/log/euserv.log
else
    # 单次运行模式
    echo "$(date): 单次运行模式..."
    /usr/local/bin/python3 /app/euser_renew.py
fi
