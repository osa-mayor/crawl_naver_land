# Local Mac Mini Migration Plan

## 1. Objective

이 문서의 목적은 현재 `.github/workflows/daily_crawl.yml`이 수행하는 전체 파이프라인을 GitHub Actions 오케스트레이션 없이 로컬 Mac mini에서 안정적으로 실행하도록 전환하는 구현 계획을 정의하는 것이다.

전환 대상은 다음 전체 흐름이다.

1. 네이버 부동산 샤드 크롤링
2. 샤드 DB 병합
3. 권역별 Excel 생성
4. Google Drive 업로드
5. Discord 알림
6. 정기 스케줄링 및 운영 로그/복구

이번 계획의 기본 원칙은 다음과 같다.

- 현재 데이터 수집 로직 자체는 최대한 유지한다.
- GitHub Actions 전용 개념만 로컬 운영 개념으로 치환한다.
- 단일 Mac mini에서 실행되는 특성을 고려해 락, 정리(cleanup), 재시도, 절전 방지, 경로 관리, 비밀값 주입을 먼저 설계한다.
- private repo 백업은 제거하고 Google Drive를 기본 백업 경로로 사용한다.
- 1차 목표는 "GitHub 없이도 현재 파이프라인을 재현"하는 것이다.


## 2. Scope

### In Scope

- `.github/workflows/daily_crawl.yml`의 로컬 대체 설계
- `crawler.py` 기반 샤드 실행 구조 설계
- `merge_db.py`, `export_db.py`, `upload_drive.py`, `send_discord.py` 재사용 구조 설계
- `launchd` 기반 스케줄링 구조 설계
- 로컬 비밀값 주입 방식 설계
- 중복 실행 방지, 실패 복구, 로그 보관, 출력물 정리 전략 설계
- cutover 및 rollback 절차 정의

### Out of Scope

- `crawler.py`의 데이터 추출 로직 전면 재작성
- 데이터 스키마 변경
- `monthly_validator.yml`의 동시 전환
- Google Drive 인증 방식을 OAuth 외 다른 방식으로 교체
- 네이버 차단 회피 로직의 근본적 재설계


## 3. Current State Inventory

### 3.1 Current Workflow

현재 메인 파이프라인은 `.github/workflows/daily_crawl.yml`에 정의돼 있다.

- 스케줄: `0 16 * * 0,3` (`.github/workflows/daily_crawl.yml:5`)
- 동시성 그룹: `daily-crawl`, `cancel-in-progress: true` (`.github/workflows/daily_crawl.yml:8`)
- 대상 러너: `self-hosted, macOS, crawl-naver-land` (`.github/workflows/daily_crawl.yml:15`)
- 샤드 그룹: 총 20개 샤드를 10개 그룹(`0-1` ~ `18-19`)으로 나눔 (`.github/workflows/daily_crawl.yml:27`)
- 매트릭스 동시 실행 수: `max-parallel: 2` (`.github/workflows/daily_crawl.yml:25`)
- 후속 단계: merge -> export -> Drive upload -> Discord notify (`.github/workflows/daily_crawl.yml:145` 이후)

즉, 현재 구조는 이미 "self-hosted Mac" 위에서 돌고 있지만, 스케줄과 오케스트레이션은 GitHub가 담당하고 있다.

### 3.2 Reusable Scripts

현재 코드베이스에는 로컬 전환에 재사용 가능한 CLI 성격의 스크립트가 이미 존재한다.

- `crawler.py`
  - 샤드 인수 지원: `--shard-index`, `--shard-total`, `--db-path` (`crawler.py:1213`~`crawler.py:1219`)
  - 로컬 환경 변수 사용: `MAX_CONCURRENT_PAGES`, `MAX_API_PREFETCH_CONCURRENCY` (`crawler.py:29`~`crawler.py:32`)
- `merge_db.py`
  - 패턴 기반 shard DB 병합 (`merge_db.py:58`~`merge_db.py:126`)
- `export_db.py`
  - `merged.db` 기반 Excel 생성 (`export_db.py:8`~`export_db.py:124`)
- `upload_drive.py`
  - `--files`, `--folder`, `--token` 입력으로 Drive 업로드 (`upload_drive.py:49`~`upload_drive.py:65`)
- `send_discord.py`
  - 메시지와 Webhook으로 알림 전송 (`send_discord.py:22`~`send_discord.py:32`)
- `test_naver_connection.py`
  - Playwright 기반 사전 연결 테스트 가능 (`test_naver_connection.py:4`~`test_naver_connection.py:28`)

### 3.3 Repo-Specific Constraints

현재 코드는 GitHub Actions의 ephemeral workspace를 암묵적으로 가정하는 부분이 있다.

- `crawler.py`는 `crawling_db.log`를 상대경로로 생성한다 (`crawler.py:43`)
- `crawler.py`는 `debug_crawler_fail_<dong>.png`를 현재 작업 디렉터리에 저장한다 (`crawler.py:271`)
- `crawler.py`는 `naver_region_codes.json`을 상대경로로 연다 (`crawler.py:757`, `crawler.py:867`, `crawler.py:1171`)
- `merge_db.py`는 `db_shard_*.db` 패턴을 그대로 병합한다 (`merge_db.py:72`)

이 때문에 로컬 persistent 환경으로 옮길 때는 "현재 작업 디렉터리"와 "이전 실행 결과물 정리"를 반드시 제어해야 한다.


## 4. Target Architecture

### 4.1 High-Level Design

목표 구조는 다음과 같다.

1. `launchd`가 정해진 시간에 로컬 wrapper를 실행한다.
2. wrapper는 프로젝트 루트와 가상환경을 준비하고, 비밀값을 안전하게 주입한다.
3. 로컬 오케스트레이터가 실행 락을 획득한다.
4. 오케스트레이터가 run directory를 생성하고 shard 실행 계획을 구성한다.
5. shard 실행이 끝나면 병합/엑셀/백업/알림을 순서대로 수행한다.
6. 성공/실패 결과를 `summary.json`에 남기고 락을 해제한다.

추천 구성 요소는 아래와 같다.

- `launchd` plist 1개
- shell wrapper 1개
- Python orchestrator 1개
- local config file 1개
- local runtime directory 1개

### 4.2 Recommended New Files

아래 파일들을 새로 추가하는 방향으로 구현한다.

- `local/orchestrator.py`
  - 전체 파이프라인 제어
- `local/config.py`
  - 설정 로딩 및 검증
- `local/lock.py`
  - 파일 기반 실행 락
- `scripts/run_local_pipeline.sh`
  - `launchd`가 호출하는 entrypoint
- `scripts/bootstrap_local_env.sh`
  - venv, dependency, Playwright bootstrap
- `.env.example`
  - 비밀값 제외 템플릿
- `launchd/com.osa-mayor.crawl-naver-land.plist`
  - 설치용 템플릿

문서 단계에서는 파일명까지 고정하고, 구현 단계에서는 이 이름을 그대로 따르는 것을 기본값으로 한다.


## 5. Scheduling Strategy

### 5.1 Why `launchd`

macOS에서는 `cron`보다 `launchd`를 기본 선택으로 한다.

이유는 다음과 같다.

- `WorkingDirectory`를 명시할 수 있다.
- stdout/stderr 로그 경로를 명시할 수 있다.
- 재부팅 후 자동 복구 및 운영 일관성이 더 좋다.
- GUI 세션/사용자 세션 기반 Mac 운영 환경과 더 잘 맞는다.

### 5.2 Default Schedule

1차 cutover에서는 현재 GitHub Actions의 cadence를 그대로 유지한다.

- 현재 액션 schedule: 일/수 16:00 UTC
- KST 기준: 월/목 01:00

즉, phase 1 기본값은 다음과 같다.

- `launchd`도 월/목 01:00 KST로 설정
- 완전 cutover 후 안정화가 끝나면 일일 실행으로 바꾸는 것은 별도 변경으로 관리

이렇게 해야 전환과 cadence 변경을 한 번에 섞지 않아 원인 추적이 쉬워진다.


## 6. Runtime Layout

로컬 persistent 환경에서는 실행 산출물을 run별로 격리해야 한다.

기본 디렉터리 구조는 아래와 같이 잡는다.

```text
/Users/pyo/Projects/crawl_naver_land
├── local/
├── scripts/
├── launchd/
├── runtime/
│   ├── locks/
│   ├── logs/
│   ├── runs/
│   │   └── <run_id>/
│   │       ├── shards/
│   │       ├── exports/
│   │       ├── logs/
│   │       ├── screenshots/
│   │       ├── merged.db
│   │       ├── failed_shards.txt
│   │       └── summary.json
│   └── latest -> runs/<run_id>
└── ...existing repo files
```

### 6.1 `run_id`

`run_id`는 KST 기준 timestamp로 생성한다.

예시:

- `2026-03-15T013000+0900`
- 또는 파일명 친화적으로 `20260315_013000_kst`

### 6.2 Output Placement

모든 shard DB는 아래 경로에 생성한다.

- `runtime/runs/<run_id>/shards/db_shard_<n>.db`

병합 결과:

- `runtime/runs/<run_id>/merged.db`

엑셀 결과:

- `runtime/runs/<run_id>/exports/export_<date>_Seoul.xlsx`
- `runtime/runs/<run_id>/exports/export_<date>_Gyeonggi.xlsx`
- `runtime/runs/<run_id>/exports/export_<date>_Metros.xlsx`
- `runtime/runs/<run_id>/exports/export_<date>_Provinces.xlsx`

스크린샷/로그도 run directory 아래로 모으는 것을 목표로 한다.


## 7. Configuration And Secrets

### 7.1 Non-Secret Config

비밀이 아닌 값은 `.env`와 분리된 일반 설정 파일로 관리한다.

추천 파일:

- `local/config.json`

필수 항목:

- `project_root`
- `runtime_root`
- `venv_path`
- `max_local_parallel`
- `max_concurrent_pages`
- `max_api_prefetch_concurrency`
- `schedule_timezone`
- `retention_days`
- `enable_drive_upload`
- `enable_discord_notify`

### 7.2 Secret Inputs

GitHub Secrets는 로컬에서 아래 값으로 대체한다.

- `GDRIVE_TOKEN`
- `GDRIVE_FOLDER_ID`
- `DISCORD_WEBHOOK_URL`

1차 구현 기본값은 다음과 같다.

- 비밀값은 `.env` 또는 macOS Keychain에서 주입
- repo에는 `.env.example`만 커밋
- 실제 `.env`는 절대 커밋하지 않음

현재 `.gitignore`에는 `credentials.json`, `token.json`, `token_output.txt` 등이 이미 제외돼 있다 (`.gitignore:23`~`.gitignore:28`). 구현 시 `.env`도 여기에 포함해야 한다.

### 7.3 Secret Validation

오케스트레이터는 장시간 크롤링을 시작하기 전에 필수 비밀값을 검증해야 한다.

필수 검증 규칙:

- Drive 업로드가 활성화돼 있으면 `GDRIVE_TOKEN`, `GDRIVE_FOLDER_ID`가 있어야 함
- Discord 알림이 활성화돼 있으면 `DISCORD_WEBHOOK_URL` 또는 webhook 파일이 있어야 함

실패 시 crawl 시작 전에 즉시 종료한다.


## 8. Local Orchestration Design

### 8.1 Execution Entry

전체 실행은 `scripts/run_local_pipeline.sh`가 담당한다.

이 wrapper의 책임은 다음과 같다.

1. 프로젝트 루트로 이동
2. 가상환경 활성화
3. `.env` 로드
4. `PYTHONUTF8=1`, `PYTHONUNBUFFERED=1` 설정
5. 오케스트레이터 실행

예상 호출 형태:

```bash
python3 -m local.orchestrator --config local/config.json
```

### 8.2 Locking

현재 GitHub Actions는 `concurrency.group`으로 중복 실행을 제어한다 (`.github/workflows/daily_crawl.yml:8`). 로컬에서는 파일 기반 락으로 대체한다.

권장 설계:

- 락 파일: `runtime/locks/daily_crawl.lock`
- 락 내용:
  - PID
  - 시작 시각
  - run_id
  - command
- 정책:
  - 락이 없으면 획득 후 실행
  - 락이 살아 있으면 새 실행을 skip하고 Discord 경고 전송
  - stale lock 판정 기준은 PID 존재 여부와 시작 시각 둘 다 확인

기본 정책은 "기존 실행 유지, 새 실행 skip"이다. GitHub처럼 무조건 cancel-in-progress를 로컬에서 흉내내지 않는다.

### 8.3 Preflight

본 실행 전에 아래 항목을 검사한다.

1. Python 실행 가능
2. 가상환경 유효
3. Playwright Chromium 설치 확인
4. `naver_region_codes.json` 존재 확인
5. `test_naver_connection.py` 또는 동일 수준의 간단 연결 체크 옵션
6. 디스크 여유 공간 확인
7. 필수 비밀값 확인
8. (선택) Discord 설정 확인

Preflight 실패 시 shard 실행을 시작하지 않는다.

### 8.4 Shard Scheduling

현재 워크플로우는 샤드 20개를 10개 그룹으로 쪼개고 그룹 단위로 2개까지 병렬 실행한다 (`.github/workflows/daily_crawl.yml:23`~`.github/workflows/daily_crawl.yml:27`).

로컬 기본 전략도 이 구조를 최대한 유지한다.

- logical shard count: 20
- local group count: 10
- group per job: 2 shards
- `max_local_parallel`: 기본 2

즉, 아래 그룹을 그대로 유지한다.

- `0-1`, `2-3`, `4-5`, `6-7`, `8-9`, `10-11`, `12-13`, `14-15`, `16-17`, `18-19`

각 worker는 그룹 안의 shard를 순차 실행한다.

예시:

```text
Worker A: 0 -> 1
Worker B: 2 -> 3
```

동시에 최대 2개의 worker만 실행한다.

### 8.5 Individual Shard Command

각 shard 실행 명령은 현재 액션과 동일하게 유지한다.

```bash
python3 crawler.py --shard-index <n> --shard-total 20 --db-path <absolute_shard_db_path>
```

단, `--db-path`는 반드시 run directory 내 절대 경로로 넘긴다.

### 8.6 Retry Policy

repo에는 현재 failed shard를 기록하는 패턴이 이미 있다 (`.github/workflows/daily_crawl.yml:71`~`.github/workflows/daily_crawl.yml:74`). 로컬도 이를 확장한다.

기본 정책:

- shard 실패 시 1회 자동 재시도
- 재시도 전 30~90초 랜덤 backoff
- 재시도 후에도 실패하면 `failed_shards.txt`에 기록
- 전체 run은 partial failure로 계속 진행하되 summary에 반영

### 8.7 Resume / Retry Failed

로컬에선 GitHub UI의 manual rerun이 없으므로 CLI 재실행 모드를 둔다.

권장 옵션:

- `--retry-failed <run_id>`
- `--resume <run_id>`
- `--shards 3 4 9`

이 기능은 1차 필수는 아니지만 plan에는 포함한다. 단일 Mac 운영에서는 실제로 매우 유용하다.


## 9. Required Code Changes Before Cutover

단순 wrapper만 추가해서는 안전하지 않다. 현재 코드 구조상 아래 변경이 선행돼야 한다.

### 9.1 Path Safety In `crawler.py`

현재 `crawler.py`는 아래 경로를 상대경로로 사용한다.

- `crawling_db.log` (`crawler.py:43`)
- `debug_crawler_fail_<dong>.png` (`crawler.py:271`)
- `naver_region_codes.json` (`crawler.py:757`, `crawler.py:867`, `crawler.py:1171`)

필수 변경:

- `PROJECT_ROOT` 또는 `BASE_DIR = Path(__file__).resolve().parent` 도입
- `REGION_JSON_PATH`를 명시적으로 계산
- 로그 파일 경로를 env 또는 CLI로 주입 가능하게 변경
- 디버그 스크린샷 저장 디렉터리를 env 또는 CLI로 주입 가능하게 변경

권장 신규 인자:

- `--log-path`
- `--screenshot-dir`
- `--region-json-path`

### 9.2 Logging Rotation

현재 `logging.FileHandler`는 무제한 로그 증가를 유발한다 (`crawler.py:43`). 로컬에서는 `RotatingFileHandler` 또는 run directory 고정 로그 파일로 교체해야 한다.

목표:

- 각 run별 `crawler.log`
- 전체 시스템용 `runtime/logs/launcher.log`

### 9.3 Deterministic Merge Inputs

현재 `merge_db.py`는 glob 패턴만 보고 병합한다 (`merge_db.py:72`). persistent 환경에서는 stale DB를 잘못 집어올 수 있다.

필수 변경 방향:

- 병합 대상은 run directory 아래 shard 경로만 허용
- 또는 orchestrator가 shard 파일 목록을 명시적으로 전달

즉, 아래 형태로 제한한다.

```bash
python3 merge_db.py "runtime/runs/<run_id>/shards/db_shard_*.db" "runtime/runs/<run_id>/merged.db"
```

### 9.4 Summary Generation

현재 GitHub Actions는 step output으로 상태를 이어받는다 (`.github/workflows/daily_crawl.yml:131`~`.github/workflows/daily_crawl.yml:143`). 로컬에서는 이를 `summary.json`으로 대체해야 한다.

`summary.json` 필수 필드:

- `run_id`
- `started_at`
- `finished_at`
- `status`
- `completed_shards`
- `failed_shards`
- `db_count`
- `merged_db_path`
- `excel_files`
- `drive_uploads`
- `discord_notification`
- `error_stage`


## 10. Post-Processing Flow

### 10.1 Merge

shard 단계 후 성공한 DB가 하나 이상 있으면 merge를 시도한다.

조건:

- `db_count > 0`이면 merge 진행
- `db_count == 0`이면 바로 failure 처리 후 Discord 경고

### 10.2 Excel Export

현재 액션과 동일하게 4개 파일을 생성한다 (`.github/workflows/daily_crawl.yml:151`~`.github/workflows/daily_crawl.yml:169`).

생성 대상:

- Seoul
- Gyeonggi
- Metros
- Provinces

정책:

- Excel export 실패는 전체 run을 partial success로 분류 가능
- `merged.db`가 살아 있으면 백업 단계는 계속 진행 가능하도록 설계

### 10.3 Google Drive Upload

현재 `upload_drive.py`는 JSON token string 또는 token file 경로를 받을 수 있다.

로컬 계획:

- `GDRIVE_TOKEN` 환경변수 또는 `.local_secrets/gdrive_token.json` 파일 사용
- `merged.db`와 생성된 Excel 파일들을 순차 업로드
- 업로드 실패 시 파일별 결과를 `summary.json`에 기록

추가로 `LOCAL_TIPS_AND_TRICKS.md`에 quota/403 관련 메모가 있으므로 (`LOCAL_TIPS_AND_TRICKS.md:70` 이후), 구현 시 지수 백오프 재시도도 고려 대상이다.

### 10.4 Discord Notification

현재 액션은 step 결과를 바탕으로 메시지를 조합한다 (`.github/workflows/daily_crawl.yml:206`~`.github/workflows/daily_crawl.yml:228`).

로컬 계획:

- 메시지 생성은 `summary.json` 기반으로 통일
- webhook은 `local/config.json` + `.local_secrets/discord_webhook_url.txt` 조합으로 관리
- 상태 분류:
  - success
  - partial_success
  - failure
  - skipped_due_to_lock


## 11. Preflight Cleanup And Retention

persistent 환경에서는 cleanup이 필수다.

### 11.1 Before Run

run 시작 전에 해야 할 일:

- 이번 run directory 생성
- 이전 `runtime/latest` symlink 갱신 준비
- stale lock 검사
- 임시 파일 영역 비우기

단, 이전 run 산출물은 바로 지우지 않는다. 보관 정책으로 이동한다.

### 11.2 After Run

run 종료 후 해야 할 일:

- `runtime/latest`를 이번 run으로 갱신
- retention 일수 지난 run directory 삭제 또는 압축
- 오래된 로그 정리

기본 retention:

- run artifacts: 14일
- system logs: 30일


## 12. Mac Mini Operational Controls

### 12.1 Sleep Prevention

Mac mini는 장시간 실행 중 절전으로 인한 중단이 없어야 한다.

현재 runner 운영에서 `caffeinate` wrapper가 이미 유효했던 경험이 있으므로, 로컬 배치도 같은 접근을 기본값으로 삼는다.

선택지:

- `launchd`가 wrapper를 호출하고 wrapper가 `caffeinate` 아래에서 오케스트레이터 실행
- 또는 시스템 전원 설정에서 절전 비활성화

기본 권장:

- 배치 실행 시점에만 `caffeinate` 사용

### 12.2 Working Directory

현재 코드의 상대경로 의존성 때문에 `launchd` plist에 `WorkingDirectory`를 반드시 넣는다.

기본값:

- `/Users/pyo/Projects/crawl_naver_land`

### 12.3 Log Paths

`launchd` stdout/stderr는 별도 파일로 저장한다.

기본값:

- `runtime/logs/launchd.stdout.log`
- `runtime/logs/launchd.stderr.log`


## 13. Validation Gates

cutover 전에 다음 검증 단계를 순차적으로 통과해야 한다.

### Gate 0: Bootstrap

- venv 생성 가능
- `pip install -r requirements.txt` 성공 (`requirements.txt`)
- `python3 -m playwright install chromium` 성공

### Gate 1: Connectivity

- `test_naver_connection.py` 성공

### Gate 2: Single Shard Smoke Test

- 샤드 1개 실행 성공
- shard DB 1개 생성
- 로그 파일 생성

### Gate 3: Small Batch

- 2~4개 shard 실행
- 병합 성공
- Excel 1개 이상 생성

### Gate 4: Full Local Run Without Scheduler

- 20 shard 전체 실행
- `merged.db` 생성
- 4개 Excel 생성
- Drive/Discord 단계 모두 시도됨

### Gate 5: Scheduled Shadow Mode

- `launchd`로 3회 연속 자동 실행 성공
- 중복 실행 없음
- stale output 오염 없음

### Gate 6: Cutover

- GitHub Actions schedule 비활성화
- 로컬 스케줄이 primary가 됨
- rollback 문서화 완료


## 14. Rollout Phases

### Phase 1: Local Driver Skeleton

- wrapper 추가
- config 로더 추가
- lock 추가
- summary.json 추가

### Phase 2: Path-Safe Refactor

- `crawler.py`의 상대경로 제거
- 로그/스크린샷/region json 경로 주입 지원

### Phase 3: Local Post-Processing

- merge/export/Drive/Discord를 orchestrator에서 연결

### Phase 4: Scheduled Execution

- `launchd` 설치
- 수동 trigger + 자동 trigger 모두 검증

### Phase 5: Shadow Mode

- GitHub schedule 유지 상태에서 로컬 병행 검증

### Phase 6: Cutover

- GitHub schedule 제거 또는 disable
- 운영 문서 최종화


## 15. Failure Classes And Handling

각 failure는 stage 단위로 명확히 분류한다.

### 15.1 Preflight Failure

- 비밀값 누락
- 네트워크 불가
- Playwright 미설치
- Discord webhook 또는 Drive token 설정 오류

대응:

- run 중단
- Discord 실패 알림
- lock 해제

### 15.2 Shard Failure

- 특정 shard만 실패
- 재시도 후에도 실패

대응:

- failed shard 기록
- partial success 가능

### 15.3 Merge Failure

- shard DB 존재하지만 merge 실패

대응:

- 후속 export/upload 중단
- 실패 알림

### 15.4 Export Failure

- merged.db는 있으나 xlsx 생성 실패

대응:

- DB 백업은 계속 가능
- partial success

### 15.5 Backup Failure

- Drive upload 실패

대응:

- local artifacts 보존
- partial success

### 15.6 Notification Failure

- Discord 전송 실패

대응:

- 전체 run status는 유지
- summary.json에 notification error 기록


## 16. Security And Secret Handling

로컬 운영으로 옮기면 GitHub Secrets 보호막이 사라진다. 따라서 최소 기준을 문서에 명시해야 한다.

- `.env`는 절대 커밋 금지
- 비밀값 출력 금지
- wrapper에서 `set -x` 금지
- 로그에 token/json dump 금지
- Discord webhook은 시크릿 파일 또는 환경변수로 관리

추가 권장:

- Google Drive token 유효성 점검용 `verify_token.py`를 cutover 전 점검 절차에 포함


## 17. Testing Strategy

구현은 문서부터가 아니라 테스트 가능 구조로 간다.

### 17.1 Unit Tests

- config parsing
- lock acquire/release/stale lock detection
- shard plan generation
- summary aggregation
- status classification

### 17.2 Integration Tests

- fixture shard DB 여러 개를 `merge_db.py`로 병합
- fixture `merged.db`를 `export_db.py`로 export
- mocked Drive upload / Discord send

### 17.3 Manual Smoke Tests

- `test_naver_connection.py`
- shard 1개 실행
- 2개 그룹 병렬 실행


## 18. Recommended Implementation Defaults

1차 구현 기본값은 아래와 같다.

- scheduler: `launchd`
- cadence: 현재 GitHub와 동일한 월/목 01:00 KST
- local parallelism: 2 worker
- shard grouping: 현재 액션과 동일한 10개 그룹
- shard retry: 1회
- lock policy: 기존 실행 유지, 새 실행 skip
- output root: `runtime/runs/<run_id>/`
- secret source: `.env` 또는 Keychain
- monthly validator: phase 1 범위 제외


## 19. Atomic Commit Plan

구현 시 커밋은 아래 단위로 나눈다.

1. `docs: add local mac mini migration plan`
2. `test: add config and lock tests for local runner`
3. `feat: add local config and secret loading`
4. `feat: add local orchestrator and run manifest`
5. `feat: make crawler paths configurable for local runs`
6. `feat: add local post-processing pipeline`
7. `feat: add launchd wrapper and plist template`
8. `test: add smoke and integration coverage for local pipeline`
9. `chore: disable GitHub schedule after local cutover`


## 20. Definition Of Done

아래 조건이 모두 만족되면 전환 완료로 본다.

- GitHub Actions 없이 Mac mini 단독으로 파이프라인 실행 가능
- 자동 스케줄 실행 가능
- 같은 run이 겹쳐서 중복 실행되지 않음
- stale shard/로그 때문에 결과가 오염되지 않음
- `merged.db`와 4개 Excel이 안정적으로 생성됨
- Drive upload / Discord notify 결과가 summary에 기록됨
- 장애 시 어디서 실패했는지 5분 안에 파악 가능
- GitHub Actions로 되돌리는 rollback 절차가 문서화돼 있음


## 21. Immediate Next Step

이 문서 기준으로 바로 다음 구현 작업은 아래 순서로 시작한다.

1. `local/config.py`, `local/lock.py`, `local/orchestrator.py` 골격 추가
2. `scripts/run_local_pipeline.sh` 추가
3. `crawler.py` 상대경로 제거 및 경로 주입 지원
4. run directory / summary.json 기반 full local dry run 구현
5. 마지막으로 `launchd` 연결
