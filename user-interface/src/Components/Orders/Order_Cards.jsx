import React from "react";
import PropTypes from "prop-types";
import "./index.css";

export default function Order_Cards(props) {
  return (
    <div className="order-card">
      <h3>
        Order_num: {props.order_num}
        price: {props.price}
        quantity: {props.quantity}
        direction: {props.direction}
      </h3>
    </div>
  );
}

Order_Cards.propTypes = {
  order_num: PropTypes.interger,
  price: PropTypes.interger,
  quantity: PropTypes.interger,
  direction: PropTypes.string,
};
