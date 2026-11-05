# 联邦 I-I2I：独立部署之间的互联 ICD（F-11）

> 依据：愿景 v0.3 §4.3、§15、§20（P2.5）、§21 F-021、F-022；任务 `fedv2-f11-i2i-test`。

## 1. 结论

- 两个独立部署（各自的数据库、各自的密钥加密主密钥、各自的私钥）可以建立连接、聊天、完成一次完整委派，断网后自动补投。两边之间只传两样东西：签名信封，以及公开的身份记录。**不共享数据库，也没有任何超级权限连接**（F-021）。
- 部署不列出任何对端主机时（默认），**完全不向外发联邦流量**；本部署内工作区之间的联邦照常工作（F-022）。
- 已验证的范围：同一台机器上的两个独立进程和两个独立数据库，经 HTTP 通信。**跨机器、跨公网、经 TLS 的真实部署还没验证**；生产环境要求对端必须是 https。

## 2. 身份如何跨部署

- 每个部署的 `GET /v1/federation/public/identities/{fid}` 公开本部署托管的工作区及其主体的签名身份记录链（父级在前）。
- 导入时逐条检查：
  1. 联邦 ID 必须由创世公钥推导而来（自证式 ID，ADR-001a），不管是谁提供的记录。
  2. 主体的记录由它自己的密钥签名；工作区的记录由它的父主体的密钥签名。
  3. 每个 key_id 必须与对应的公钥相符。
  4. 已托管在本部署的身份永远不会被导入覆盖；导入的密钥版本只能前进。
- 导入后存为 `hosted = false`：只有公钥，没有私钥，也没有本地映射。

## 3. 对端登记（运营人员）

- `POST /v1/admin/federation/peers {federation_id, base_url, operator}`：登记对端工作区所在的部署，并立即抓取、校验、导入它的身份。双方部署都要登记对方。
- 只有 `federation_peer_hosts` 里列出的主机能被访问（支持 `*.example.com`）；不在列表里的返回 `FED_E_EGRESS_DISABLED`。默认列表为空。
- 生产环境只接受 https 的 `base_url`。

## 4. 投递

- 发往不在本部署托管的工作区的信封，由传输层 POST 到对端的 `/v1/federation/inbox`。
- 对端收件箱：
  1. 先按“收件工作区 + 自称发送方”限流。
  2. 再走与本地完全相同的验签、授权和业务检查。
  3. 发送方未知，或报告密钥不再有效时，如果该发送方已登记，就重新抓取一次它的身份记录（密钥可能轮换了），然后重试。
- 对端拒绝时返回联邦错误码，发送方据此判断是否重试。网络不可达记为 `FED_E_UNREACHABLE`，按原有退避重试，到上限后进死信。

## 5. 安全边界

- 收件箱不需要登录：能不能进来，由签名、已登记的对端身份和限流共同决定。不认识的发送方一律拒绝。
- 对端的身份记录如果对不上（ID 不由公钥推导、签名不对、key_id 不符），拒绝导入，返回 `FED_E_REMOTE_INVALID`。

## 6. 限制（如实记录）

- 只验证了同机双进程；跨机器、公网、反向代理和 TLS 证书的部署演练属于上线前的运维工作。
- 身份记录的撤销与轮换会在下一次投递失败时刷新；目前还没有定时同步。
- 收件箱的限流按单个进程计数（与 ICD_I_MSG §5 相同）。

## 7. 测试

`tests/test_federation_i2i.py`：
- `test_two_deployments_work_together_with_nothing_shared_but_envelopes`（F-021）
- `test_an_outage_between_deployments_is_caught_up`
- `test_forged_records_and_unknown_issuers_are_refused`
- `test_a_closed_network_sends_nothing_out_and_still_works`（F-022）

## 8. 错误码

`FED_E_EGRESS_DISABLED`（403）、`FED_E_REMOTE_INVALID`（422）。
