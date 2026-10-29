// Реквизиты продавца. Заполните один раз: они подставятся на все страницы сайта
// (главная, оферта, политика персональных данных). Банк сверяет их при проверке сайта.
const SELLER = {
  name: "ИП Кретушев Денис Анатольевич",
  inn: "524615798116",
  ogrnip: "326527500160442",
  city: "г. Нижний Новгород",
  email: "topdefnik@gmail.com",
  phone: "+7 930 709-68-62",
  tg: "timecode_shop",
  site: "ander7i.github.io/my-first-project",
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
