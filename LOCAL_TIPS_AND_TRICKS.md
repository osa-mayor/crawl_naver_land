# 🛠️ Naver Land Crawler - Engineering Notes

> **Note**: 이 문서는 프로젝트 개발 과정에서 얻은 기술적 노하우, 트러블슈팅 사례, 그리고 성능 최적화 기법을 정리한 **엔지니어링 노트**입니다.

---

## 🏗️ 1. 대규모 크롤링 아키텍처 (Scaling Architecture)

### 🛑 문제 상황 (The Problem)
*   **Target**: 전국 3,000개 이상의 행정동(Dong).
*   **Constraint**: GitHub Actions의 무료 티어는 실행 시간 제한(6시간)과 동시 실행 제한이 있음.
*   **Bottleneck**: 단일 프로세스로 실행 시 약 5~6시간 소요되며, 중간에 네트워크 오류 발생 시 전체 재시작 필요.

### ✅ 해결 전략: "Divide and Conquer" (Sharding)
단순한 멀티스레딩이 아니라, **인프라 레벨의 병렬 처리**를 구현했습니다.

1.  **Dynamic Sharding (Striped Partitioning)**
    *   초기에는 앞뒤로 자르는 "Block Partitioning"을 시도했으나, 앞쪽(서울/경기)만 데이터가 엄청나고 뒤쪽(강원/제주)은 금방 끝나는 **불균형(Skew)** 발생.
    *   **Modulo Sharding** 도입: `Region_Index % Total_Shards` 로직으로 무작위 분산 효과. 서울과 지방이 골고루 섞여 모든 워커(Worker)가 비슷한 시간에 종료됨. (`crawler.py:get_sharded_targets`)

2.  **Matrix Strategy**
    *   GitHub Actions의 `matrix` 기능을 활용해 20개의 독립된 가상 머신(VM)을 띄움.
    *   전체 실행 시간: **25~30분** (단일 실행 대비 12배 속도 향상).

---

## 🕵️ 2. 봇 탐지 우회 기술 (Anti-Bot Evasion)

네이버 부동산은 `Playwright`나 `Selenium` 같은 자동화 도구를 적극적으로 차단합니다. 이를 뚫기 위한 핵심 설정입니다.

### 🛡️ Stealth Techniques
1.  **`AutomationControlled` 플래그 제거**
    *   브라우저가 열릴 때 `navigator.webdriver = true` 값을 숨겨야 합니다.
    *   `args=["--disable-blink-features=AutomationControlled"]` 필수 적용.
2.  **Real User-Agent Rotation**
    *   단순히 "Headless Chrome"이라고 뜨면 바로 차단됩니다.
    *   최신 Windows/Mac의 Chrome 버전을 랜덤하게 로테이션하여 사용.
3.  **Human-like Behavior**
    *   **Random Sleep**: `sleep(1)` 대신 `sleep(random.uniform(0.5, 1.5))` 사용. 기계적인 주기성(Regularity) 제거.
    *   **Viewport**: 1920x1080 고정. 작은 뷰포트는 봇으로 의심받기 쉽고 모바일 레이아웃을 로딩시킬 위험이 있음.

---

## 💾 3. 데이터 정합성 (Data Consistency)

### 🔄 Upsert 패턴 (Idempotency)
크롤러는 언제든 재실행될 수 있어야 합니다(Idempotent).
*   **Complexes**: `INSERT OR REPLACE` 사용. 이미 존재하는 단지는 최신 정보로 덮어쓰고, 없으면 생성.
*   **Prices**: 기본적으로 `INSERT` 하되, 동일 날짜/단지/평형 데이터 중복 방지를 위해 복합 인덱스 활용 고려.

### 🧩 Merge Logic
분산된 20개의 DB(`shard_0.db` ~ `shard_19.db`)를 합칠 때의 전략:
*   SQLite의 `ATTACH DATABASE` 기능을 사용하여 하나의 쿼리로 대량 데이터를 이동.
*   Python 레벨의 Loop보다 SQL 레벨의 Merge가 월등히 빠르고 안전함.

---

## 🐛 4. 트러블슈팅 사례 (Troubleshooting Log)

### 🚨 Case 1: 데이터 누락 (Missing Data)
*   **현상**: 크롤러는 정상 종료되었는데 DB가 비어있음.
*   **원인**: 비동기(`async`) 구문 안에서 데이터를 모으기만 하고, 브라우저가 닫히기 직전에 `save_to_db()`를 호출하지 않음.
*   **해결**: `finally` 블록 또는 루프 종료 직후 명시적 `save()` 호출 추가.

### 🚨 Case 2: CSS Selector 변경
*   **현상**: 잘 되던 크롤러가 갑자기 요소를 못 찾음.
*   **원인**: 네이버가 React/Vue 등의 모던 프레임워크로 업데이트하면서 클래스명이 동적 해시(`class="Article_article__2kM7..."`)로 변경됨.
*   **해결**: 특정 클래스명에 의존하지 않고, **계층 구조**(`div > a`)나 **속성**(`href*="/complexes/"`) 기반의 견고한(Robust) 선택자로 교체.

### 🚨 Case 3: Google Drive API Quota
*   **현상**: 파일 업로드 실패 (403 Forbidden).
*   **원인**: 서비스 계정의 저장 용량 초과가 아니라, **API 호출 빈도 제한** 걸림.
*   **해결**: 청크 업로드(Resumable Upload) 방식 적용 및 실패 시 지수 백오프(Exponential Backoff) 재시도 로직 추가.
