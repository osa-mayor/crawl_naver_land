# 🏙️ Naver Land Crawler Project Info

> **Note**: 이 문서는 프로젝트의 상세 명세와 수집 데이터 구조를 다루는 로컬 전용 문서입니다. 보안 및 상세 구현 내용을 포함하고 있으므로 GitHub 원격 저장소에는 동기화하지 않습니다.

---

## 1. 🎯 프로젝트 개요 (Introduction)
이 프로젝트는 네이버 부동산(Naver Land)의 아파트 매물 및 시세 정보를 매일 자동으로 수집하여 데이터베이스화하는 시스템입니다.
특히 **갭 투자(Gap Investment)** 분석을 위해 매매가와 전세가의 차이(Gap)를 추적하고, 대규모 지역(전국 단위)의 데이터를 **병렬 처리**로 빠르게 수집하는 데 최적화되어 있습니다.

### 핵심 목표
1.  **전국 단위 데이터 수집**: 서울, 경기, 인천, 부산 등 주요 도시 포함 3,000개 이상의 읍/면/동 데이터 커버.
2.  **안정성 및 자동화**: GitHub Actions를 통해 매일 새벽 사람의 개입 없이 자동 실행.
3.  **데이터 정규화**: 단지 정보와 시세 변동을 분리하여 효율적으로 저장.
4.  **분석 친화적 결과**: 엑셀(Excel) 및 SQL DB 형태로 제공하여 즉시 분석 가능.a

---

## 2. 📊 수집 데이터 명세 (Data Schema)

데이터는 SQLite 데이터베이스(`merged.db`)에 두 개의 정규화된 테이블로 저장됩니다.

### 🏢 A. 단지 정보 테이블 (`complexes`)
변하지 않는 아파트/오피스텔의 **기본 하드웨어 스펙**입니다. (`complex_no`를 Primary Key로 사용)

| 필드명 (Field) | 타입 | 설명 | 예시 |
| :--- | :--- | :--- | :--- |
| `complex_no` | Integer | 네이버 단지 고유 ID (PK) | 12345 |
| `name` | Text | 단지명 | 반포자이 |
| `region_depth1` | Text | 시/도 | 서울시 |
| `region_depth2` | Text | 시/군/구 | 서초구 |
| `region_depth3` | Text | 읍/면/동 | 반포동 |
| `total_households` | Integer | 총 세대수 | 3410 |
| `total_dongs` | Integer | 총 동수 | 44 |
| `completion_date` | Text | 준공년월 | 200903 |
| `construction_company`| Text | 건설사 | GS건설 |
| `heating_method` | Text | 난방 방식 | 지역난방 |
| `heating_fuel` | Text | 난방 연료 | 열병합 |
| `parking_per_household`| Real | 세대당 주차 대수 | 1.78 |
| `far` | Real | 용적률 (%) | 270.0 |
| `bcr` | Real | 건폐율 (%) | 13.0 |
| `latitude` | Real | 위도 | 37.507... |
| `longitude` | Real | 경도 | 127.011... |
| `last_updated` | Timestamp | 정보 갱신 시각 (Upsert용) | 2024-01-24... |

### 📈 B. 시세 정보 테이블 (`prices`)
매일 변동되는 **시장 가격 및 매물 데이터**입니다. (`complex_no` + `date` + `pyeong_type`이 복합 키 역할)

| 필드명 (Field) | 타입 | 설명 | 비고 |
| :--- | :--- | :--- | :--- |
| `id` | Integer | 고유 ID (Auto) | |
| `complex_no` | Integer | 단지 ID (FK) | complexes 테이블 참조 |
| `date` | Text | 수집 일자 | YYYY-MM-DD |
| `pyeong_type` | Text | 평형 타입 | 84A, 59B 등 |
| `supply_area` | Real | 공급 면적 (㎡) | 112.5 |
| `exclusive_area` | Real | 전용 면적 (㎡) | 84.9 |
| `hallway_type` | Text | 현관 구조 | 계단식, 복도식 |
| `room_bath` | Text | 방/욕실 수 | 3/2 |
| `trade_min_std` | Integer | **매매** 최저가 (일반) | (단위: 만원) |
| `trade_min_low` | Integer | **매매** 최저가 (저층) | 1층, 2층 등 저층 매물 |
| `trade_max` | Integer | **매매** 최고가 | |
| `trade_avg` | Integer | **매매** 평균가 | (최저+최고)/2 아님, 네이버 기준 |
| `trade_count` | Integer | **매매** 매물 수 | 현재 올라온 매물 개수 |
| `rent_min` | Integer | **전세** 최저가 | (단위: 만원) |
| `rent_max` | Integer | **전세** 최고가 | |
| `rent_avg` | Integer | **전세** 평균가 | |
| `rent_count` | Integer | **전세** 매물 수 | |
| `gap` | Integer | **갭 (Gap)** | 매매(일반/저층) - 전세(최저) |
| `jeonse_ratio` | Real | **전세가율** (%) | (전세최저 / 매매기준) * 100 |

---

## 3. 🏗️ 기술 스택 및 아키텍처

### ⚙️ Core Technology
*   **Language**: Python 3.9+
*   **Engine**: Playwright (Headless Chrome)
*   **Infra**: GitHub Actions (Ubuntu-latest runners)
*   **Storage**: SQLite (Raw Data), Google Drive (Archive)

### 🧩 System Architecture
1.  **Sharding (분할)**: 3,000+ 지역을 20개의 Shard로 분할 (Modulo Hashing).
2.  **Parallel Execution (병렬)**: GitHub Actions Matrix 기능을 사용하여 20개의 Job 동시 실행.
3.  **Merging (병합)**: 각 Job이 생성한 `db_shard_N.db`를 Artifact로 업로드 후, 최종 단계에서 하나로 병합.
4.  **Export (추출)**: 병합된 DB에서 서울, 경기, 지방 등 권역별 엑셀 리포트 생성 및 구글 드라이브 업로드.

```mermaid
graph TD
    Trigger[🕒 Daily Schedule] --> Split[🧩 Sharding (20 shards)]
    Split --> |Shard 0..19| Matrix[🔥 Parallel Crawlers]
    
    subgraph "GitHub Actions Runners"
        Matrix
    end
    
    Matrix --> |Upload| Artifacts[📦 DB Fragments]
    Artifacts --> Merge[🔄 Merge DB]
    Merge --> |merged.db| Generate[📊 Excel Reports]
    Generate --> |Backup| GDrive[☁️ Google Drive]
    Generate --> |Alert| Discord[🔔 Notification]
```

## 4. 📁 프로젝트 파일 구조
```
Project Root
├── � crawler.py           # 크롤링 메인 로직 (Playwright)
├── 📜 API_Helpers
│   ├── fetch_region_codes.py  # 지역 코드(Region Code) 수집
│   ├── land_selectors.py      # CSS 선택자 상수 모음
│   └── region_validator.py    # 지역 유효성 검증
├── 📜 Data_Processing
│   ├── init_db              # DB 스키마 초기화 (in crawler.py)
│   ├── merge_db.py          # 분할 DB 병합 스크립트
│   └── export_db.py         # DB -> Excel 변환 스크립트
├── 📜 Workflows (.github)
│   ├── daily_crawl.yml      # 메인 크롤링 파이프라인
│   └── monthly_validator.yml # 월간 데이터/지역 검증
├── 📜 Docs (Local Only)
│   ├── LOCAL_PROJECT_INFO.md   # 본 문서
│   └── LOCAL_TIPS_AND_TRICKS.md # 기술 노하우
└── 📄 requirements.txt      # 의존성 패키지
```
