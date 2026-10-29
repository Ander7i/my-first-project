// Реквизиты продавца. Заполните один раз: они подставятся на все страницы сайта
// (главная, оферта, политика персональных данных). Банк сверяет их при проверке сайта.
const SELLER = {
  name: "ИП Фамилия Имя Отчество",
  inn: "000000000000",
  ogrnip: "000000000000000",
  city: "г. Москва",
  email: "hello@example.ru",
  phone: "+7 900 000-00-00",
  tg: "timecode_shop",
  site: "timecode.example",
};

document.querySelectorAll("[data-seller]").forEach(el => {
  const v = SELLER[el.dataset.seller];
  if (v) el.textContent = el.dataset.seller === "tg" ? "@" + v : v;
});
document.querySelectorAll("a[data-seller-href]").forEach(el => {
  const k = el.dataset.sellerHref;
  el.href = k === "email" ? "mailto:" + SELLER.email
    : k === "phone" ? "tel:" + SELLER.phone.replace(/[^\d+]/g, "")
    : "https://t.me/" + SELLER.tg;
});
