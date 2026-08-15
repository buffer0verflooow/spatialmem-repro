-- linksee-server MySQL 初始化
-- 创建数据库和专用用户。表结构由应用启动时 create_all() 自动生成。

CREATE DATABASE IF NOT EXISTS linksee
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS 'linksee'@'%' IDENTIFIED BY 'linksee';
GRANT ALL PRIVILEGES ON linksee.* TO 'linksee'@'%';
FLUSH PRIVILEGES;
